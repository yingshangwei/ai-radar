"""Saved article card headings and social identity; read endpoints never call models."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from .math_text import technical_document
from .models import (
    Article,
    ArticleDocument,
    ArticleReading,
    DocumentAnalysis,
    SummaryReview,
    WebDocument,
)
from .official_news import NEWS_IDS
from .ranking import canonicalize
from .reading import analysis_key
from .schemas import DirectReference
from .summary_evidence import review_evidence_fingerprint
from .summary_review import SummaryReviewPending, SummaryReviewService

MAX_ARTICLES_PER_JOB = 3


def technical_reading_owned(session, article, config):
    """One technical title comes from the same reviewed source as its explanation."""
    if (not config.reading.enabled or config.provider.kind == "extractive" or article.platform not in {"web", "rss"}
            or not technical_document(article.title, article.text)):
        return False
    return any(doc.title == article.title and doc.text == article.text
               and canonicalize(doc.url) == canonicalize(article.url)
               for doc in session.scalars(select(WebDocument).join(ArticleDocument).where(
                   ArticleDocument.article_id == article.id, ArticleDocument.relation == "source")))


def article_evidence(article):
    evidence = {name: getattr(article, name) for name in (
        "id", "title", "text", "url", "author", "handle", "published_at", "published_precision")}
    if article.source_id in {"hf-papers", "arxiv-theory"}:
        evidence.update(partial=True, evidence_type="paper_abstract")
    elif article.source_id in NEWS_IDS:
        evidence.update(partial=True, evidence_type="source_excerpt")
    return evidence


def source_heading(session, article, config, *, full=False):
    """Only the article's own source page may provide its existing AI heading."""
    if article.platform not in {"web", "rss"}:
        return None
    docs = session.scalars(select(WebDocument).join(ArticleDocument).where(
        ArticleDocument.article_id == article.id, ArticleDocument.relation == "source"))
    for doc in docs:
        if (canonicalize(doc.url) != canonicalize(article.url) or not doc.text
                or article.title != doc.title or article.text != doc.text):
            continue
        if doc.analysis_id != analysis_key(session, doc, config):
            continue
        analysis = session.get(DocumentAnalysis, doc.analysis_id)
        if (analysis and analysis.status == "ready" and analysis.provider != "extractive"
                and analysis.title_zh.strip()):
            return ({"status": "ready", "title_zh": analysis.title_zh,
                     "summary_zh": analysis.summary_zh, "key_points_zh": analysis.key_points_zh,
                     "why_it_matters_zh": analysis.why_it_matters_zh}
                    if full else analysis.title_zh)
    return None


def presentation_views(session, articles, config):
    """Only current, fully approved cache data is exposed; no model construction."""
    reviews = SummaryReviewService(None, config)
    result = {}
    for article in articles:
        uid = article.id
        source = source_heading(session, article, config, full=True)
        if source:
            result[uid] = source
            continue
        if not config.summary_review.enabled or config.provider.kind == "extractive":
            result[uid] = {"status": "disabled", "title_zh": None}
            continue
        evidence = [article_evidence(article)]
        try:
            key, _, _ = reviews._identity("documents", "article:" + uid, evidence, evidence)
            row = session.get(SummaryReview, key)
            status = row.status if row else "pending"
            if row and row.status == "ready":
                state = {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}
                if reviews._approved(state):
                    docs = state["candidate"]["documents"]
                    if len(docs) == 1 and docs[0]["source_id"] == uid:
                        result[uid] = {"status": "ready", **{key: docs[0].get(key) for key in (
                            "title_zh", "summary_zh", "key_points_zh", "why_it_matters_zh")}}
                        continue
                status = "review_required"
            result[uid] = {"status": status if status in {"pending", "review_required", "error"} else "pending",
                           "title_zh": None}
        except (SummaryReviewPending, ValueError, TypeError, KeyError):
            result[uid] = {"status": "review_required", "title_zh": None}
    return result


def social_views(session, article_ids):
    result = {uid: {"reply_to": None} for uid in article_ids}
    for row in session.scalars(select(ArticleReading).where(ArticleReading.article_id.in_(article_ids))):
        for raw in row.references:
            if raw.get("kind") != "reply":
                continue
            try:
                ref = DirectReference.model_validate(raw)
            except (ValueError, TypeError):
                continue
            result[row.article_id]["reply_to"] = {
                "url": ref.url, "author": ref.author, "handle": ref.handle,
                "published_at": ref.published_at.isoformat() if ref.published_at else None,
            }
            break
    return result


class ArticlePresentationService:
    def __init__(self, sessions, config, provider_factory):
        self.sessions, self.config, self.provider_factory = sessions, config, provider_factory
        self.reviews = SummaryReviewService(sessions, config)

    def _pending_items(self, limit=None):
        from .admission import visible_clause
        if not self.config.summary_review.enabled or self.config.provider.kind == "extractive":
            return []
        chosen = []
        with self.sessions() as session:
            cutoff = (datetime.now(UTC) - timedelta(hours=self.config.lookback_hours)).isoformat()
            articles = list(session.scalars(select(Article).where(Article.published_at >= cutoff, visible_clause())
                                           .order_by(Article.published_at.desc(), Article.id)))
            views = presentation_views(session, articles, self.config)
            for article in articles:
                if views[article.id]["status"] == "ready":
                    continue
                evidence = [article_evidence(article)]
                sources = deepcopy(evidence)
                if self.reviews.can_analyze_documents("article:" + article.id, sources, evidence=evidence):
                    chosen.append((article.id, sources, evidence))
                if len(chosen) == min(limit or MAX_ARTICLES_PER_JOB, self.config.provider.max_items):
                    break
        return chosen

    def has_pending(self):
        return bool(self._pending_items(limit=1))

    async def pending(self, *, limit=None):
        chosen = self._pending_items(limit)
        if not chosen:
            return {"processed": 0, "ready": 0}
        provider = self.provider_factory(self.config.provider)
        ready = 0
        for uid, sources, evidence in chosen:
            try:
                await self.reviews.analyze_documents("article:" + uid, sources, provider, evidence=evidence)
                # The service cache is bound to frozen evidence. Source changes
                # during the call stay private and are hidden by the read view.
                with self.sessions() as session:
                    current = session.get(Article, uid)
                    if current and review_evidence_fingerprint([article_evidence(current)]) == \
                            review_evidence_fingerprint(evidence):
                        ready += 1
            except SummaryReviewPending:
                continue  # Existing durable budgets/unknown-result rules own all recovery.
        return {"processed": len(chosen), "ready": ready}
