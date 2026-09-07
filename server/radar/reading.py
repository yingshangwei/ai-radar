"""Durable, one-hop source reading shared by collection, mobile and daily summaries."""
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from .config import RadarConfig
from .links import mentioned_references, normalize_link, text_references, useful_link
from .models import (
    Article,
    ArticleDocument,
    ArticleReading,
    DocumentAnalysis,
    Translation,
    WebDocument,
    now_iso,
)
from .page_parser import extract_page
from .providers import make_provider
from .translation import cache_key
from .web_reader import PageFetcher, PageUnavailable


def fingerprint(*values) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def remember_references(session, article: Article, references: list[dict]):
    row = session.get(ArticleReading, article.id)
    if row:
        row.references = references
    else:
        session.add(ArticleReading(article_id=article.id, references=references))


def sync_documents(session, article: Article, config: RadarConfig):
    source = session.get(ArticleReading, article.id)
    seeds = []
    root_url = normalize_link(article.url)
    if article.platform in {"rss", "web"} and root_url:
        seeds.append({"url": root_url, "label": article.title[:300], "relation": "source"})
    direct = list(source.references if source else [])
    expanded = {normalize_link(ref.get("short_url", "")) for ref in direct
                if ref.get("short_url") and ref["short_url"] != ref["url"]}
    direct += [ref for ref in text_references(article.text) if ref["url"] not in expanded]
    # Only the SOURCE page can contribute first-hop links. Child document links are never expanded.
    root = session.get(WebDocument, fingerprint(root_url)) if seeds else None
    if root:
        direct += [ref for ref in root.links if not (
            urlsplit(ref['url']).hostname == urlsplit(root.final_url or root.url).hostname
            and urlsplit(ref['url']).path.rstrip('/') in {'', '/blog', '/news', '/index'})]
    mentions = mentioned_references(article.title + "\n" + article.text, config.reading.mention_catalog)
    if root:
        mentions += mentioned_references(root.text, config.reading.mention_catalog)
    seen = {root_url} if seeds else set()
    child_count = 0
    for candidate in direct + mentions:
        url = normalize_link(candidate["url"], root_url or article.url)
        if not url or url in seen or not useful_link(url):
            continue
        seen.add(url)
        seeds.append({"url": url, "label": candidate.get("label", "")[:300],
                      "relation": candidate.get("relation", "link")})
        child_count += 1
        if child_count >= config.reading.max_links_per_article:
            break
    wanted = set()
    for ref in seeds:
        key = fingerprint(ref["url"])
        wanted.add(key)
        if not session.get(WebDocument, key):
            session.add(WebDocument(id=key, url=ref["url"]))
            session.flush()
        binding = session.get(ArticleDocument, (article.id, key))
        if binding:
            binding.label, binding.relation = ref["label"], ref["relation"]
        else:
            session.add(ArticleDocument(article_id=article.id, document_id=key,
                                        label=ref["label"], relation=ref["relation"]))
    for binding in session.scalars(select(ArticleDocument).where(ArticleDocument.article_id == article.id)):
        if binding.document_id not in wanted:
            session.delete(binding)


def resource_views(session, article_ids: list[str], config, *, full=False) -> dict[str, list[dict]]:
    bindings = session.execute(select(ArticleDocument, WebDocument).join(WebDocument).where(
        ArticleDocument.article_id.in_(article_ids))).all()
    analyses = {a.id: a for a in session.scalars(select(DocumentAnalysis).where(
        DocumentAnalysis.id.in_({doc.analysis_id for _, doc in bindings})))}
    translation_ids = {cache_key(doc.title, doc.text, config) for _, doc in bindings if doc.text}
    translated = {t.id: t for t in session.scalars(select(Translation).where(Translation.id.in_(translation_ids)))}
    result = {uid: [] for uid in article_ids}
    seen = {uid: set() for uid in article_ids}
    for binding, doc in sorted(bindings, key=lambda row: (row[0].relation != "source", row[1].url)):
        canonical = doc.final_url or doc.url
        if canonical in seen[binding.article_id]:
            continue
        seen[binding.article_id].add(canonical)
        analysis = analyses.get(doc.analysis_id)
        ready = analysis is not None and analysis.status == "ready"
        zh = translated.get(cache_key(doc.title, doc.text, config)) if config.enabled and doc.text else None
        zh_ready = zh is not None and zh.status == "ready"
        view = {
            "id": doc.id, "url": doc.url, "resolved_url": canonical, "relation": binding.relation,
            "title": doc.title or binding.label or urlsplit(doc.url).hostname,
            "title_zh": analysis.title_zh if ready else (zh.title_zh if zh_ready else None),
            "status": "ready" if ready else ("analysis_error" if analysis and analysis.status == "error"
                                               else ("pending" if doc.text else doc.status)),
            "fetch_status": doc.status, "message": doc.message, "partial": doc.partial,
            "fetched_at": doc.fetched_at or None,
            "summary_zh": analysis.summary_zh if ready else None,
            "key_points_zh": analysis.key_points_zh if ready else [],
            "why_it_matters_zh": analysis.why_it_matters_zh if ready else None,
            "translation_status": zh.status if zh else ("pending" if config.enabled else "disabled"),
        }
        if full:
            view.update(text=doc.text, text_zh=zh.text_zh if zh_ready else None)
        result[binding.article_id].append(view)
    return result


class ReadingService:
    def __init__(self, sessions, config: RadarConfig, translations):
        self.sessions, self.config, self.translations = sessions, config, translations
        self.semaphore = asyncio.Semaphore(config.reading.concurrency)

    async def fetch_one(self, key: str, fetcher: PageFetcher):
        async with self.semaphore:
            with self.sessions() as session:
                doc = session.get(WebDocument, key)
                url, etag, modified = doc.url, doc.etag, doc.modified
            headers = {}
            if etag:
                headers["If-None-Match"] = etag
            if modified:
                headers["If-Modified-Since"] = modified
            try:
                final, code, response_headers, body = await fetcher.bytes(url, headers=headers)
                parsed = None if code == 304 else await extract_page(
                    body, response_headers.get("content-type", "").lower(), final)
                with self.sessions.begin() as session:
                    row = session.get(WebDocument, key)
                    if parsed is not None:
                        row.title = parsed["title"] or urlsplit(final).hostname or "网页正文"
                        row.text, row.links, row.partial = parsed["text"], parsed["links"], parsed["partial"]
                        row.content_hash = fingerprint(row.title, row.text, row.partial)
                        row.final_url, row.content_type = final, response_headers.get("content-type", "")
                        row.etag, row.modified = response_headers.get("etag", ""), response_headers.get("last-modified", "")
                    if not row.text:
                        raise ValueError("304 without cached content")
                    row.status, row.message, row.fetched_at = "fetched", "", now_iso()
                    row.retry_at = (datetime.now(UTC) + timedelta(hours=self.config.reading.refresh_hours)).isoformat()
            except (PageUnavailable, httpx.HTTPError, OSError, ValueError, TimeoutError) as exc:
                from .browser_access import browser_fallback, save_capture
                if not isinstance(exc, PageUnavailable) or exc.status not in {"blocked", "restricted", "too_large", "rate_limited"}:
                    try:
                        result = await browser_fallback(self.sessions, url)
                        if result:
                            with self.sessions.begin() as session:
                                save_capture(session, session.get(WebDocument, key), result,
                                             self.config.reading.refresh_hours)
                            return
                    except (PageUnavailable, ValueError) as browser_error:
                        exc = browser_error
                with self.sessions.begin() as session:
                    row = session.get(WebDocument, key)
                    row.status = exc.status if isinstance(exc, PageUnavailable) else "unavailable"
                    row.message = exc.message if isinstance(exc, PageUnavailable) else "暂未取得可阅读正文，可能为失效链接、扫描件或动态页面。"
                    row.retry_at = (datetime.now(UTC) + timedelta(hours=6)).isoformat()

    async def pending(self, *, force=False) -> dict:
        if not self.config.reading.enabled:
            return {"enabled": False}
        with self.sessions.begin() as session:
            articles = session.scalars(select(Article).order_by(Article.published_at.desc())).all()
            for article in articles:
                sync_documents(session, article, self.config)
            ids = [a.id for a in articles]
        fetcher = PageFetcher()
        fetched = set()
        # Two fixed phases: source/explicit targets first, source-body references second. No traversal loop.
        for _phase in range(2):
            remaining = self.config.reading.max_documents - len(fetched)
            if remaining <= 0:
                break
            with self.sessions.begin() as session:
                for article in session.scalars(select(Article).where(Article.id.in_(ids))):
                    sync_documents(session, article, self.config)
                query = select(WebDocument).join(ArticleDocument).join(Article).where(
                    Article.id.in_(ids)).order_by(Article.published_at.desc(), ArticleDocument.relation != "source")
                candidates = session.scalars(query).all()
                keys = list(dict.fromkeys(d.id for d in candidates if d.id not in fetched
                                         and (d.retry_at <= now_iso() or (force and not d.text))))[:remaining]
            fetched.update(keys)
            await asyncio.gather(*(self.fetch_one(key, fetcher) for key in keys))
        with self.sessions.begin() as session:
            for article in session.scalars(select(Article).where(Article.id.in_(ids))):
                sync_documents(session, article, self.config)
            docs = session.scalars(select(WebDocument).join(ArticleDocument).join(Article).where(
                WebDocument.text != "").order_by(Article.published_at.desc())).unique().all()
            payloads, seen = [], set()
            for doc in docs:
                key = fingerprint(doc.content_hash, self.config.reading.revision)
                doc.analysis_id = key
                analysis = session.get(DocumentAnalysis, key)
                if not analysis:
                    analysis = DocumentAnalysis(id=key)
                    session.add(analysis)
                    session.flush()
                # Translation also resumes for saved pages without requiring another network fetch.
                if key in seen:
                    continue
                seen.add(key)
                needs_analysis = analysis.status != "ready" and (force or analysis.retry_at <= now_iso())
                translation = session.get(Translation, cache_key(doc.title, doc.text, self.config.translation))
                needs_translation = self.config.translation.enabled and (not translation or (
                    translation.status != "ready" and (force or (translation.retry_at <= now_iso()
                    and translation.attempts < self.config.translation.max_attempts))))
                if not needs_analysis and not needs_translation:
                    continue
                payloads.append({"id": key, "url": doc.final_url or doc.url, "title": doc.title,
                                 "text": doc.text, "partial": doc.partial,
                                 "needs_analysis": needs_analysis})
        payloads = payloads[:self.config.reading.max_documents]
        payloads = await self.translations.evidence(payloads, force=force)
        to_analyze = [{k: v for k, v in doc.items() if k != "needs_analysis"}
                      for doc in payloads if doc["needs_analysis"]]
        completed = 0
        provider = make_provider(self.config.provider)
        for offset in range(0, len(to_analyze), 4):
            batch = to_analyze[offset:offset + 4]
            try:
                output = await provider.analyze(batch)
                with self.sessions.begin() as session:
                    for summary in output.documents:
                        row = session.get(DocumentAnalysis, summary.source_id)
                        for name, value in summary.model_dump(exclude={"source_id"}).items():
                            setattr(row, name, value)
                        row.status, row.provider, row.model = "ready", self.config.provider.kind, self.config.provider.model or ""
                        row.updated_at, row.retry_at = now_iso(), ""
                        completed += 1
            except Exception:
                with self.sessions.begin() as session:
                    for doc in batch:
                        row = session.get(DocumentAnalysis, doc["id"])
                        row.status = "error"
                        row.retry_at = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
                        row.updated_at = now_iso()
        return {"enabled": True, "fetched": len(fetched), "summarized": completed}

    def evidence(self, articles: list[dict]) -> list[dict]:
        with self.sessions() as session:
            resources = resource_views(session, [a["id"] for a in articles], self.config.translation)
        return [dict(a, evidence_type="source_excerpt", resources=[
            {k: v for k, v in resource.items() if k in {
                "url", "resolved_url", "relation", "title", "title_zh", "summary_zh", "key_points_zh",
                "why_it_matters_zh", "partial", "fetched_at"}}
            for resource in resources[a["id"]] if resource["status"] == "ready"
        ]) for a in articles]
