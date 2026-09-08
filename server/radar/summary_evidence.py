"""Read saved, directly bound source evidence without fetching or derived analysis.

Bodies are never truncated here. The workflow's prompt budget can reject an
oversized packet without presenting an incomplete packet as a complete source.
"""

import hashlib
import json

from sqlalchemy import select

from .config import RadarConfig
from .models import Article, ArticleDocument, WebDocument
from .ranking import canonicalize
from .summary_contracts import DirectResource, FrozenSource


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ids(items: list[dict]) -> list[str]:
    ids = []
    for item in items:
        uid = item.get("id") if isinstance(item, dict) else None
        if not isinstance(uid, str) or not uid.strip() or len(uid) > 240:
            raise ValueError("Review evidence requires valid source IDs")
        ids.append(uid)
    if len(ids) != len(set(ids)):
        raise ValueError("Review evidence source IDs must be unique")
    return ids


def freeze_review_evidence(sources: list[dict]) -> list[dict]:
    """Stable one-hop schema whitelist; never retain candidate/approval metadata."""
    _ids(sources)
    frozen = []
    for source in sources:
        packet = FrozenSource.model_validate(source).model_dump()
        if not packet["text"].strip():
            raise ValueError("Review evidence requires original source text")
        _ids(packet["resources"])
        if any(not item["text"].strip() for item in packet["resources"]):
            raise ValueError("Review resources require original source text")
        packet["resources"].sort(key=lambda item: (item["id"], _json(item)))
        frozen.append(packet)
    return sorted(frozen, key=lambda item: item["id"])


def review_evidence_fingerprint(sources: list[dict]) -> str:
    """Content/provenance hash, independent of fetch time, metrics and ordering."""
    return hashlib.sha256(_json(freeze_review_evidence(sources)).encode()).hexdigest()


def reserve_publication(session) -> None:
    """Reserve SQLite publication before re-reading evidence and writing results.

    Keep this transaction open through the final fingerprint comparison and
    publication. Other backends need an explicit equivalent locking contract.
    """
    if session.get_bind().dialect.name != "sqlite":
        raise RuntimeError("Summary publication locking requires the supported SQLite backend")
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _saved_body_available(row) -> bool:
    if not row["document_text"] or not row["document_text"].strip():
        return False
    if row["document_status"] == "fetched":
        return True
    # A failed refresh leaves the last successful body and metadata intact.
    # Match the title/text/partial hash written by ReadingService/save_capture
    # before treating an older successful snapshot as usable source evidence.
    if not row["document_fetched_at"]:
        return False
    previous = json.dumps(
        [row["document_title"], row["document_text"], row["document_partial"]],
        ensure_ascii=False, sort_keys=True,
    )
    return row["document_content_hash"] == hashlib.sha256(previous.encode()).hexdigest()


def digest_review_evidence(session, articles: list[dict], config: RadarConfig) -> list[dict]:
    """Rebuild exactly the selected articles from one consistent database query.

    Selected payloads contribute IDs only. Current bound resources are retained
    in full, source-first and canonically deduplicated per article. There is no
    second link cap that could omit evidence already supplied to generation.
    """
    ids = _ids(articles)
    if len(ids) > config.provider.max_items:
        raise ValueError("Selected review sources exceed the configured article budget")
    if not ids:
        return []
    statement = select(
        Article.id.label("article_id"), Article.title, Article.text, Article.url,
        Article.author, Article.handle, Article.published_at, Article.published_precision,
    ).where(Article.id.in_(ids))
    if config.reading.enabled:
        statement = statement.outerjoin(ArticleDocument, ArticleDocument.article_id == Article.id).outerjoin(
            WebDocument, WebDocument.id == ArticleDocument.document_id,
        ).add_columns(
            WebDocument.id.label("document_id"), WebDocument.url.label("document_url"),
            WebDocument.final_url.label("document_final_url"), WebDocument.title.label("document_title"),
            WebDocument.text.label("document_text"), WebDocument.partial.label("document_partial"),
            WebDocument.status.label("document_status"), WebDocument.fetched_at.label("document_fetched_at"),
            WebDocument.content_hash.label("document_content_hash"), ArticleDocument.relation,
        )
    # Column results bypass mutable identity-map objects; no autoflush may turn
    # a read into a write or substitute an uncommitted candidate for saved text.
    with session.no_autoflush:
        rows = session.execute(statement).mappings().all()
    packets, resources = {}, {}
    for row in rows:
        uid = row["article_id"]
        if uid not in packets:
            packets[uid] = {
                "id": uid, "title": row["title"], "text": row["text"], "url": row["url"],
                "author": row["author"], "handle": row["handle"], "published_at": row["published_at"],
                "published_precision": row["published_precision"], "partial": False,
                "evidence_type": "source_excerpt", "resources": [],
            }
            resources[uid] = []
        if not config.reading.enabled or row["document_id"] is None or not _saved_body_available(row):
            continue
        url = row["document_final_url"] or row["document_url"]
        resources[uid].append((canonicalize(url), {
            "id": row["document_id"], "url": url, "title": row["document_title"],
            "text": row["document_text"], "partial": row["document_partial"],
            "evidence_type": "publisher_article", "relation": row["relation"],
        }))
    if set(packets) != set(ids):
        raise ValueError("Selected review sources are no longer available")
    output = []
    for uid in ids:
        seen = set()
        for canonical, resource in sorted(
            resources[uid], key=lambda item: (item[1]["relation"] != "source", item[0], item[1]["id"]),
        ):
            if canonical in seen:
                continue
            seen.add(canonical)
            packets[uid]["resources"].append(resource)
        packet = FrozenSource.model_validate(packets[uid]).model_dump()
        if not packet["text"].strip():
            raise ValueError("Selected review source has no original text")
        output.append(packet)
    return output


def document_review_evidence(documents: list[dict]) -> list[dict]:
    """Clean an existing reading batch, retaining its exact IDs and raw bodies.

    A document under review does not gain evidence by traversing its resources.
    The caller supplies the saved reading batch; this function performs no I/O.
    """
    _ids(documents)
    result = []
    for doc in documents:
        source = DirectResource.model_validate({"evidence_type": "publisher_article", **doc}).model_dump()
        if not source["text"].strip():
            raise ValueError("Document review requires original source text")
        result.append({**source, "resources": []})
    return result
