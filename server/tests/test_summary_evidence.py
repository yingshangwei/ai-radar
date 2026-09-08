import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from radar.config import ProviderConfig, RadarConfig, ReadingConfig
from radar.db import database
from radar.models import Article, ArticleDocument, DocumentAnalysis, WebDocument
from radar.reading import fingerprint
from radar.summary_contracts import FrozenSource
from radar.summary_evidence import (
    digest_review_evidence,
    document_review_evidence,
    freeze_review_evidence,
    reserve_publication,
    review_evidence_fingerprint,
)


@pytest.fixture
def store(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/summary-evidence.db")
    yield engine, sessions
    engine.dispose()


def config(*, enabled=True, max_items=35):
    return RadarConfig(reading=ReadingConfig(enabled=enabled), provider=ProviderConfig(max_items=max_items))


def article(sessions, uid="a", *, text="Original AI source text.", **kwargs):
    values = {
        "id": uid, "platform": "x", "source_id": "x", "external_id": uid,
        "url": f"https://x.com/original/status/{uid}", "canonical_url": f"https://x.com/original/status/{uid}",
        "title": f"Original AI title {uid}", "text": text,
        "author": f"Original author {uid}", "handle": f"original_{uid}",
        "published_at": "2026-09-08T00:00:00+00:00", "published_precision": "date",
    } | kwargs
    with sessions.begin() as session:
        session.add(Article(**values))
    return uid


def document(sessions, uid="doc", *, article_ids=("a",), relation="link", text=None, **kwargs):
    text = f"Original saved page body {uid}." if text is None else text
    values = {
        "id": uid, "url": f"https://example.org/{uid}", "title": f"Original page title {uid}",
        "text": text, "partial": False, "status": "fetched", "fetched_at": "2026-09-08T00:00:00+00:00",
    } | kwargs
    values.setdefault("content_hash", fingerprint(values["title"], values["text"], values["partial"]))
    with sessions.begin() as session:
        session.add(WebDocument(**values))
        session.flush()
        for article_id in article_ids:
            session.add(ArticleDocument(article_id=article_id, document_id=uid, relation=relation))
    return uid


def read(sessions, ids=("a",), *, settings=None):
    with sessions() as session:
        return digest_review_evidence(session, [{"id": uid} for uid in ids], settings or config())


def test_selected_ids_only_rebuild_saved_originals_ignoring_payload_and_derived_analyses(store):
    engine, sessions = store
    article(sessions, "a")
    article(sessions, "b", text="UNSELECTED-PRIVATE-ORIGINAL")
    document(sessions, "doc", partial=True)
    with sessions.begin() as session:
        session.add(DocumentAnalysis(id="derived", title_zh="DERIVED-TITLE", summary_zh="DERIVED-SUMMARY",
                                     key_points_zh=["DERIVED-POINT"], status="ready"))
        session.get(WebDocument, "doc").analysis_id = "derived"
    queries = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        queries.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record)
    supplied = [{"id": "a", "title": "TAMPERED-TITLE", "text": "TAMPERED-CANDIDATE",
                 "title_zh": "UNREVIEWED-ZH", "resources": [{"text": "TAMPERED-RESOURCE"}]}]
    before = deepcopy(supplied)
    try:
        with sessions() as session:
            result = digest_review_evidence(session, supplied, config())
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(queries) == 1 and queries[0].startswith("select")
    assert "document_analyses" not in queries[0] and "translations" not in queries[0]
    source = result[0]
    assert source["id"] == "a" and source["text"] == "Original AI source text."
    assert source["title"] == "Original AI title a" and source["author"] == "Original author a"
    assert source["handle"] == "original_a" and source["published_precision"] == "date"
    assert source["published_at"] == "2026-09-08T00:00:00+00:00"
    resource = source["resources"][0]
    assert resource["text"] == "Original saved page body doc." and resource["partial"]
    assert resource["author"] == resource["handle"] == resource["published_at"] == ""
    assert resource["relation"] == "link" and resource["evidence_type"] == "publisher_article"
    assert all(marker not in json.dumps(result) for marker in [
        "UNSELECTED-PRIVATE", "DERIVED-", "TAMPERED-", "UNREVIEWED-",
    ])
    assert supplied == before


def test_source_resource_wins_canonical_duplicate_and_direct_bindings_never_expand_links(store):
    _, sessions = store
    article(sessions)
    article(sessions, "other")
    document(sessions, "alias", final_url="https://www.example.org/report/?utm_source=alias")
    document(sessions, "source", relation="source", final_url="https://example.org/report",
             links=[{"url": "https://example.org/deeper", "relation": "link"}])
    document(sessions, "direct", relation="mention", links=[{"url": "https://example.org/deeper"}])
    document(sessions, "deeper", article_ids=("other",), text="DEEPER-PRIVATE-TEXT")
    document(sessions, "orphan", article_ids=(), text="ORPHAN-PRIVATE-TEXT")

    source = read(sessions)[0]

    assert [item["id"] for item in source["resources"]] == ["source", "direct"]
    assert all("resources" not in item and "links" not in item for item in source["resources"])
    assert "DEEPER-PRIVATE" not in json.dumps(source) and "ORPHAN-PRIVATE" not in json.dumps(source)


def test_deduplication_is_per_article_and_input_article_order_is_preserved(store):
    _, sessions = store
    article(sessions, "a")
    article(sessions, "b")
    document(sessions, "shared", article_ids=("a", "b"))
    result = read(sessions, ("b", "a"))
    assert [source["id"] for source in result] == ["b", "a"]
    assert [source["resources"][0]["id"] for source in result] == ["shared", "shared"]


def test_reading_disabled_queries_only_article_and_does_not_use_payload_resources(store):
    engine, sessions = store
    article(sessions)
    document(sessions)
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record)
    try:
        result = read(sessions, settings=config(enabled=False))
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert result[0]["resources"] == []
    assert len(statements) == 1
    assert "article_documents" not in statements[0] and "web_documents" not in statements[0]


@pytest.mark.parametrize("status", ["unavailable", "auth_required", "rate_limited", "access_restricted"])
def test_failed_refresh_keeps_verified_previous_successful_body_as_saved_evidence(store, status):
    _, sessions = store
    article(sessions)
    document(sessions, status=status)
    resource = read(sessions)[0]["resources"][0]
    assert resource["text"] == "Original saved page body doc."
    assert "status" not in resource and "fetched_at" not in resource


@pytest.mark.parametrize("fault", ["no_success", "wrong_hash", "empty", "whitespace"])
def test_body_without_usable_saved_provenance_or_text_is_not_promoted(store, fault):
    _, sessions = store
    article(sessions)
    kwargs = {"status": "auth_required"}
    if fault == "no_success":
        kwargs["fetched_at"] = ""
    elif fault == "wrong_hash":
        kwargs["content_hash"] = "not-current-body"
    else:
        kwargs["status"] = "fetched"
        kwargs["text"] = "" if fault == "empty" else " \n\t "
    document(sessions, **kwargs)
    assert read(sessions)[0]["resources"] == []


def test_entire_saved_body_and_partial_flag_are_preserved_without_another_link_or_character_cap(store):
    _, sessions = store
    article(sessions)
    full = "原始正文。" * 14_000
    document(sessions, "long", text=full, partial=True)
    document(sessions, "second")
    settings = config()
    settings.reading.max_links_per_article = 1
    result = read(sessions, settings=settings)[0]
    assert len(result["resources"]) == 2
    long = next(item for item in result["resources"] if item["id"] == "long")
    assert long["text"] == full and long["partial"]


@pytest.mark.parametrize("ids", [("missing",), ("a", "a"), ("",), (None,)])
def test_missing_or_ambiguous_selected_ids_fail_without_inventing_evidence(store, ids):
    _, sessions = store
    article(sessions)
    with pytest.raises(ValueError):
        read(sessions, ids)


def test_selected_article_budget_and_empty_input_are_explicit(store):
    _, sessions = store
    article(sessions, "a")
    article(sessions, "b")
    with pytest.raises(ValueError, match="budget"):
        read(sessions, ("a", "b"), settings=config(max_items=1))
    assert read(sessions, ()) == []


def test_read_does_not_flush_or_trust_uncommitted_identity_map_edits(store):
    _, sessions = store
    article(sessions)
    document(sessions)
    with sessions() as session:
        pending = session.get(Article, "a")
        pending_doc = session.get(WebDocument, "doc")
        pending.text = "UNCOMMITTED-CANDIDATE"
        pending_doc.text = "UNCOMMITTED-PAGE"
        result = digest_review_evidence(session, [{"id": "a"}], config())
        assert result[0]["text"] == "Original AI source text."
        assert result[0]["resources"][0]["text"] == "Original saved page body doc."
        assert pending in session.dirty
    assert read(sessions) == result


def test_document_cleaner_keeps_raw_reading_batch_only_with_no_links_or_nested_evidence():
    documents = [{"id": "analysis-id", "title": "Original title", "text": "Original document body.",
                  "url": "https://example.org/report", "partial": True,
                  "title_zh": "DERIVED-PRIVATE-TITLE", "summary_zh": "DERIVED-PRIVATE-SUMMARY",
                  "review": {"approved": True}, "resources": [{"id": "deeper", "text": "DEEPER-PRIVATE"}]}]
    before = deepcopy(documents)
    result = document_review_evidence(documents)
    assert result[0]["id"] == "analysis-id" and result[0]["text"] == documents[0]["text"]
    assert result[0]["partial"] and result[0]["resources"] == []
    assert result[0]["evidence_type"] == "publisher_article"
    assert "DERIVED-PRIVATE" not in json.dumps(result) and "DEEPER-PRIVATE" not in json.dumps(result)
    assert documents == before
    assert FrozenSource.model_validate(result[0]).model_dump() == result[0]


@pytest.mark.parametrize("documents", [
    [{"id": "a", "text": " "}], [{"id": "a", "text_zh": "仅有译文"}],
    [{"id": "a", "text": "body"}, {"id": "a", "text": "body"}],
])
def test_document_cleaner_rejects_empty_raw_body_or_duplicate_ids(documents):
    with pytest.raises(ValueError):
        document_review_evidence(documents)


def test_fingerprint_ignores_dynamic_fields_order_and_derived_content_but_not_actual_facts(store):
    _, sessions = store
    article(sessions, "a")
    article(sessions, "b")
    document(sessions, "one")
    document(sessions, "two")
    before = read(sessions, ("a", "b"))
    altered = deepcopy(list(reversed(before)))
    for source in altered:
        source.update(metrics={"like_count": 999}, score=999, fetched_at="2099-01-01", title_zh="PRIVATE-ZH")
        source["resources"].reverse()
        for resource in source["resources"]:
            resource.update(status="unavailable", summary_zh="PRIVATE-SUMMARY", fetched_at="2099-01-01")
    assert review_evidence_fingerprint(before) == review_evidence_fingerprint(altered)
    assert freeze_review_evidence(before) == freeze_review_evidence(altered)
    for name, value in {"text": "Different original", "author": "Different author",
                        "published_at": "2026-09-09", "published_precision": "timestamp"}.items():
        changed = deepcopy(before)
        changed[0][name] = value
        assert review_evidence_fingerprint(changed) != review_evidence_fingerprint(before)
    for name, value in {"text": "Different saved page", "partial": True, "relation": "source"}.items():
        changed = deepcopy(before)
        changed[0]["resources"][0][name] = value
        assert review_evidence_fingerprint(changed) != review_evidence_fingerprint(before)


def test_publication_reservation_blocks_real_concurrent_writer_until_transaction_finishes(store):
    engine, sessions = store
    article(sessions)
    document(sessions)

    def write_source():
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA busy_timeout=50")
                connection.exec_driver_sql("UPDATE web_documents SET text=? WHERE id=?", ("Concurrent source", "doc"))
            return "committed"
        except OperationalError as exc:
            assert "locked" in str(exc).lower()
            return "locked"

    with sessions.begin() as session, ThreadPoolExecutor(max_workers=1) as workers:
        reserve_publication(session)
        before = digest_review_evidence(session, [{"id": "a"}], config())
        # A distinct connection attempts a real write while the source check
        # and publication transaction retain their reservation.
        assert workers.submit(write_source).result(timeout=2) == "locked"
        assert digest_review_evidence(session, [{"id": "a"}], config()) == before
    assert write_source() == "committed"
    assert review_evidence_fingerprint(read(sessions)) != review_evidence_fingerprint(before)


def test_unsupported_publication_backend_fails_explicitly_instead_of_claiming_a_lock():
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    with pytest.raises(RuntimeError, match="SQLite"):
        reserve_publication(session)


def test_evidence_queries_leave_original_and_analysis_rows_unchanged(store):
    _, sessions = store
    article(sessions)
    document(sessions)
    with sessions() as session:
        before = {model.__tablename__: [dict(row) for row in session.execute(select(model.__table__)).mappings()]
                  for model in (Article, WebDocument, ArticleDocument, DocumentAnalysis)}
        first = digest_review_evidence(session, [{"id": "a"}], config())
        assert digest_review_evidence(session, [{"id": "a"}], config()) == first
        after = {model.__tablename__: [dict(row) for row in session.execute(select(model.__table__)).mappings()]
                 for model in (Article, WebDocument, ArticleDocument, DocumentAnalysis)}
        assert before == after
