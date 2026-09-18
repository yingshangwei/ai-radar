"""Read-only freshness and completed translation archive for authenticated readers."""
import hashlib
from datetime import UTC, datetime

from sqlalchemy import func, select

from .admission import visible_clause
from .models import (
    Article,
    ArticleDocument,
    ArticleTranslation,
    Job,
    Translation,
    Watch,
    WebDocument,
    XCollectionState,
)
from .translation import cache_key
from .x_data_config import state_key


def freshness_status(session, config):
    now = datetime.now(UTC)
    rows = list(session.scalars(select(Watch.handle).where(Watch.enabled.is_(True), Watch.platform == "x")))
    # Collection keys use watch:<handle>, while Watch.id uses x:<handle>.
    states = {s.id: s.data for s in session.scalars(select(XCollectionState))}
    ages = []
    missing = 0
    for handle in rows:
        end = states.get(state_key(config, "watch:" + handle.lower()), {}).get("head_end")
        if not end:
            missing += 1
            continue
        ages.append(max(0, (now - datetime.fromisoformat(end.replace("Z", "+00:00"))).total_seconds()))
    latest = session.scalar(select(Job).where(Job.kind == "collect").order_by(Job.started_at.desc()).limit(1))
    return {"collect_minutes": config.collect_minutes, "target_minutes": 30,
        "last_collection_at": latest.finished_at or latest.started_at if latest else None,
        "latest_article_at": session.scalar(select(func.max(Article.collected_at))),
        "watched_accounts": len(rows), "missing_accounts": missing,
        "oldest_window_minutes": round(max(ages) / 60, 1) if ages else None,
        "overdue_accounts": missing + sum(age > 35 * 60 for age in ages),
        "summary_independent": True, "original_first": True}


def translation_updates(session, config, *, limit=50):
    """Only approved current bindings. Shared source caches produce one event per article/cache."""
    articles = list(session.scalars(select(Article).where(visible_clause())))
    cache = {t.id: t for t in session.scalars(select(Translation).where(Translation.status == "ready"))}
    events = {}
    def add(article, row, resource=None):
        if row is None:
            return
        key = hashlib.sha256(f"{article.id}:{row.id}:{row.updated_at}".encode()).hexdigest()
        events[key] = {"id": key, "article_id": article.id, "completed_at": row.updated_at,
            "title": article.title, "title_zh": row.title_zh,
            "kind": "web_translation" if resource else "article_translation",
            "resource_id": resource, "author": article.author}
    by_id = {a.id: a for a in articles}
    bindings = dict(session.execute(select(ArticleTranslation.article_id, ArticleTranslation.translation_id)).all())
    for article in articles:
        key = cache_key(article.title, article.text, config.translation)
        if bindings.get(article.id) == key:
            add(article, cache.get(key))
    for doc, aid in session.execute(select(WebDocument, ArticleDocument.article_id).join(
            ArticleDocument, ArticleDocument.document_id == WebDocument.id)):
        if aid not in by_id or not doc.text:
            continue
        row = cache.get(cache_key(doc.title, doc.text, config.translation))
        # Prefer the article event when the source is also its own linked webpage.
        if row and cache_key(by_id[aid].title, by_id[aid].text, config.translation) != row.id:
            add(by_id[aid], row, doc.id)
    result = sorted(events.values(), key=lambda e: (e['completed_at'], e['id']), reverse=True)
    return {"items": result[:limit], "total": len(result),
            "latest_at": result[0]['completed_at'] if result else None}
