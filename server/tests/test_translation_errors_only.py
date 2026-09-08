import asyncio
import copy
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from radar import cli, translation
from radar.config import RadarConfig, TranslationConfig
from radar.db import database
from radar.models import Article, ArticleDocument, ArticleTranslation, Job, Translation, WebDocument
from radar.translation import AuditedPart, TranslatedPart, TranslationService, cache_key, ensure_translation
from radar.translation_recheck import translate_limited


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    engine, sessions = database(f"sqlite:///{tmp_path}/errors.db")
    yield sessions
    engine.dispose()


def snapshot(sessions, model):
    with sessions() as session:
        return {row.id if hasattr(row, "id") else (row.article_id, getattr(row, "document_id", "")):
                copy.deepcopy({column.name: getattr(row, column.name) for column in model.__table__.columns})
                for row in session.scalars(select(model))}


def article(session, config, name, *, status="error", binding=True, text=None, hour=12):
    text = text or f"AI supports {name} research."
    row = Article(
        id=name, platform="x", source_id="test", external_id=name, url=f"https://example.org/{name}",
        canonical_url=f"https://example.org/{name}", title=text, text=text, author="Researcher",
        published_at=f"2026-09-08T{hour:02}:00:00+00:00",
    )
    session.add(row)
    if status is not None:
        cached = ensure_translation(session, text, text, config)
        cached.status = status
        session.flush()
        if binding:
            session.add(ArticleTranslation(article_id=row.id, translation_id=cached.id))
    return row


def document(session, config, name, parent, *, status="error", text=None, bound=True):
    text = text or f"AI supports {name} research."
    row = WebDocument(id=name, url=f"https://example.org/{name}", title=text, text=text)
    session.add(row)
    session.flush()
    if bound:
        session.add(ArticleDocument(article_id=parent.id, document_id=row.id, relation="link"))
    if status is not None:
        ensure_translation(session, text, text, config).status = status
    return row


async def record_retryable_errors(sessions, config, keys, monkeypatch):
    """Create completed transport failures through the real durable workflow."""
    earlier = datetime.now(UTC) - timedelta(hours=2)

    class EarlierDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return earlier.astimezone(tz) if tz else earlier.replace(tzinfo=None)

    async def timeout(parts, *, review):
        raise TimeoutError("synthetic completed timeout")

    # These are initial fixtures, not unknown production failures being reopened.
    with sessions.begin() as session:
        for key in dict.fromkeys(keys):
            session.get(Translation, key).status = "pending"
    with monkeypatch.context() as clock:
        clock.setattr(translation, "datetime", EarlierDateTime)
        clock.setattr(translation, "now_iso", lambda: earlier.isoformat())
        service = TranslationService(sessions, config)
        service.request = timeout
        for key in dict.fromkeys(keys):
            await service.translate_one(key)
    with sessions() as session:
        for key in dict.fromkeys(keys):
            row = session.get(Translation, key)
            assert row.status == "error" and not row.owner and not row.lease_until
            assert row.parts[0]["workflow_history"][-1]["outcome"] == "known_transport"


@pytest.fixture
def model(monkeypatch):
    calls = []

    async def request(service, parts, *, review):
        calls.extend(("review" if review else "draft", part["source"]) for part in parts)
        return {part["id"]: TranslatedPart(id=part["id"], zh="人工智能支持研究。", approved=True)
                for part in parts}

    async def audit(service, parts):
        calls.extend(("audit", part["source"]) for part in parts)
        return {part["id"]: AuditedPart(id=part["id"], approved=True) for part in parts}

    monkeypatch.setattr(TranslationService, "request", request)
    monkeypatch.setattr(TranslationService, "audit", audit)
    return calls


@pytest.mark.asyncio
async def test_errors_only_preserves_nonerror_unbound_stale_and_missing_caches(store, model, monkeypatch):
    config = TranslationConfig(enabled=True, max_documents=100)
    with store.begin() as session:
        main = article(session, config, "main", status="ready")
        current = document(session, config, "current", main)
        for status in ["pending", "review_required", "ready", "insufficient_balance", "running"]:
            article(session, config, "article-" + status, status=status)
            document(session, config, "document-" + status, main, status=status)
        article(session, config, "missing", status=None)
        article(session, config, "unbound", binding=False)
        stale = article(session, config, "stale")
        stale.title = stale.text = "AI supports updated findings."
        document(session, config, "missing-page", main, status=None)
        document(session, config, "orphan", main, bound=False)
        stale_doc = document(session, config, "stale-page", main)
        stale_doc.title = stale_doc.text = "AI supports changed discoveries."
        current_key = cache_key(current.title, current.text, config)
        source = current.text
    await record_retryable_errors(store, config, [current_key], monkeypatch)
    before = snapshot(store, Translation)
    originals = {kind: snapshot(store, kind) for kind in [Article, WebDocument, ArticleTranslation, ArticleDocument]}
    config_before = config.model_dump()

    result = await translate_limited(store, config, limit=100, force=True, errors_only=True)

    assert result["status"] == "completed" and result["errors_only"] is True
    assert model == [(stage, source) for stage in ["draft", "review", "audit"]]
    after = snapshot(store, Translation)
    assert after[current_key]["status"] == "ready"
    assert {key: row for key, row in after.items() if key != current_key} == {
        key: row for key, row in before.items() if key != current_key}
    assert set(after) == set(before)
    assert {kind: snapshot(store, kind) for kind in originals} == originals
    assert config.model_dump() == config_before


@pytest.mark.asyncio
async def test_main_priority_and_shared_key_dedup_before_limiting(store, model, monkeypatch):
    config = TranslationConfig(enabled=True)
    with store.begin() as session:
        main = article(session, config, "main", hour=10)
        article(session, config, "shared", text=main.text, hour=9)
        recent = article(session, config, "recent", status="ready", hour=15)
        document(session, config, "a-shared-page", recent, text=main.text)
        page = document(session, config, "b-page", recent)
        document(session, config, "c-later", recent)
        expected = [main.text, page.text]
    with store() as session:
        keys = list(session.scalars(select(Translation.id).where(Translation.status == "error")))
    await record_retryable_errors(store, config, keys, monkeypatch)
    result = await translate_limited(store, config, limit=2, errors_only=True)
    assert result["status"] == "completed"
    assert [source for stage, source in model if stage == "draft"] == expected
    assert len(model) == 6
    assert result["counts"] == {"ready": 3}
    assert result["resource_counts"] == {"ready": 2, "error": 1}


@pytest.mark.asyncio
async def test_eligible_before_limit_and_force_keeps_lease_gate(store, model, monkeypatch):
    config = TranslationConfig(enabled=True)
    with store.begin() as session:
        main = article(session, config, "main", status="review_required")
        docs = [document(session, config, name, main)
                for name in ["a-leased", "b-exhausted", "c-backoff", "d-eligible"]]
        keys = [cache_key(doc.title, doc.text, config) for doc in docs]
        sources = [doc.text for doc in docs]
    await record_retryable_errors(store, config, keys, monkeypatch)
    with store.begin() as session:
        session.get(Translation, keys[0]).lease_until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        session.get(Translation, keys[0]).owner = "other-worker"
        session.get(Translation, keys[1]).attempts = config.max_attempts
        session.get(Translation, keys[2]).retry_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    before = snapshot(store, Translation)

    await translate_limited(store, config, limit=1, errors_only=True)
    assert [source for stage, source in model if stage == "draft"] == [sources[3]]
    model.clear()
    await translate_limited(store, config, limit=2, errors_only=True, force=True)
    assert [source for stage, source in model if stage == "draft"] == sources[1:3]
    after = snapshot(store, Translation)
    assert after[keys[0]] == before[keys[0]]
    assert after[keys[1]]["attempts"] == config.max_attempts + 1
    main_key = next(key for key, row in before.items() if row["status"] == "review_required")
    assert after[main_key] == before[main_key]


@pytest.mark.asyncio
@pytest.mark.parametrize("new_status", ["pending", "review_required", "ready"])
async def test_atomic_claim_rechecks_error_status_after_read(store, model, new_status, monkeypatch):
    config = TranslationConfig(enabled=True)
    with store.begin() as session:
        main = article(session, config, "main")
        key = cache_key(main.title, main.text, config)
    await record_retryable_errors(store, config, [key], monkeypatch)
    before = snapshot(store, Translation)[key]
    engine = store.kw["bind"]
    raced = []

    def change_before_claim(conn, cursor, statement, parameters, context, executemany):
        if not raced and statement.startswith("UPDATE translations SET") and "owner=" in statement:
            raced.append(True)
            # Simulate an intervening committed status change after the ORM read,
            # immediately before the compare-and-set claim reaches the database.
            cursor.execute("UPDATE translations SET status=? WHERE id=?", (new_status, key))

    event.listen(engine, "before_cursor_execute", change_before_claim)
    try:
        await TranslationService(store, config).translate_one(key, force=True, errors_only=True)
    finally:
        event.remove(engine, "before_cursor_execute", change_before_claim)
    assert raced and model == []
    assert snapshot(store, Translation)[key] == {**before, "status": new_status}


@pytest.mark.asyncio
async def test_empty_error_scope_completes_without_creating_cache_or_binding(store, model):
    config = TranslationConfig(enabled=True)
    with store.begin() as session:
        main = article(session, config, "main", status=None)
        document(session, config, "page", main, status=None)
    result = await translate_limited(store, config, limit=1, force=True, errors_only=True)
    assert result["status"] == "completed" and result["errors_only"] is True
    assert model == [] and snapshot(store, Translation) == {} and snapshot(store, ArticleTranslation) == {}
    assert snapshot(store, Job)[result["job_id"]]["status"] == "completed"


@pytest.mark.asyncio
async def test_semantic_rejection_is_preserved_and_not_retried_by_next_error_batch(store, model, monkeypatch):
    config = TranslationConfig(enabled=True)
    with store.begin() as session:
        main = article(session, config, "main")
        key = cache_key(main.title, main.text, config)
    await record_retryable_errors(store, config, [key], monkeypatch)

    async def reject(service, parts):
        return {part["id"]: AuditedPart(id=part["id"], approved=False, issues=["候选含义与原文不一致"])
                for part in parts}

    monkeypatch.setattr(TranslationService, "audit", reject)
    result = await translate_limited(store, config, limit=1, force=True, errors_only=True)
    assert result["status"] == "completed" and result["counts"] == {"review_required": 1}
    before = snapshot(store, Translation)
    assert before[key]["status"] == "review_required" and before[key]["issues"]
    assert before[key]["text_zh"] == ""
    calls = list(model)

    again = await translate_limited(store, config, limit=1, force=True, errors_only=True)
    assert again["status"] == "completed" and again["counts"] == {"review_required": 1}
    assert snapshot(store, Translation) == before and model == calls


@pytest.mark.asyncio
async def test_running_job_blocks_errors_only_without_changing_job_or_cache(store, model):
    config = TranslationConfig(enabled=True)
    with store.begin() as session:
        article(session, config, "main")
        session.add(Job(kind="read"))
    before, jobs = snapshot(store, Translation), snapshot(store, Job)
    with pytest.raises(RuntimeError, match="已有任务"):
        await translate_limited(store, config, limit=1, force=True, errors_only=True)
    assert snapshot(store, Translation) == before and snapshot(store, Job) == jobs and model == []


@pytest.mark.parametrize("args", [
    ["translate", "--errors-only"], ["collect", "--errors-only"], ["init", "--errors-only"],
    ["read", "--limit", "1", "--errors-only"],
    ["translate", "--limit", "1", "--errors-only", "--file", "prose.txt"],
    ["translate", "--limit", "1", "--errors-only", "--date", "2026-09-08"],
    ["translate", "--limit", "1", "--errors-only", "--recheck-article", "main"],
    ["translate", "--limit", "1", "--errors-only", "--recheck-editorial"],
])
def test_cli_invalid_scope_rejected_before_loading_config(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["radar", *args])
    monkeypatch.setattr(cli, "Settings", lambda: pytest.fail("Invalid scope must not load configuration"))
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2


def test_cli_errors_only_uses_formal_job_and_no_pipeline(store, model, monkeypatch, capsys):
    config = RadarConfig(translation=TranslationConfig(enabled=True))
    with store.begin() as session:
        article(session, config.translation, "semantic", status="review_required")
        main = article(session, config.translation, "technical")
        source = main.text
        key = cache_key(main.title, main.text, config.translation)
    asyncio.run(record_retryable_errors(store, config.translation, [key], monkeypatch))

    class Configured:
        database_url = str(store.kw["bind"].url)

        def load(self):
            return config

    monkeypatch.setattr(cli, "Settings", Configured)
    monkeypatch.setattr(cli, "Pipeline", lambda *args: pytest.fail("Errors-only must not initialize Pipeline"))
    monkeypatch.setattr(sys, "argv", ["radar", "translate", "--limit", "1", "--errors-only", "--force"])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed" and result["errors_only"] is True
    assert model == [(stage, source) for stage in ["draft", "review", "audit"]]
    assert result["counts"] == {"ready": 1, "review_required": 1}
    assert snapshot(store, Job)[result["job_id"]]["kind"] == "translate"
