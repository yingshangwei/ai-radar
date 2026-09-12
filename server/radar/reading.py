"""Durable, one-hop source reading shared by collection, mobile and daily summaries."""
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from .config import RadarConfig
from .links import mentioned_references, normalize_link, text_references, useful_link
from .math_text import technical_document
from .models import (
    Article,
    ArticleDocument,
    ArticleReading,
    DocumentAnalysis,
    DocumentCapture,
    Translation,
    WebDocument,
    now_iso,
)
from .page_parser import extract_page
from .providers import make_provider
from .summary_evidence import document_review_evidence, reserve_publication, review_evidence_fingerprint
from .summary_review import SummaryReviewPending, SummaryReviewService, SummaryReviewYield
from .translation import cache_key
from .web_reader import PageFetcher, PageUnavailable

RESEARCH_SOURCES = frozenset({"hf-papers", "arxiv-theory"})
RESEARCH_ABSTRACT_PREFIX = "arXiv preprint · Author abstract\n\n"
ARXIV_BASE_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})", re.I)


def research_source_url(article: Article) -> str | None:
    """Recognize the narrowly defined official-API paper ingestion contract."""
    if article.platform != "web" or article.source_id not in RESEARCH_SOURCES:
        return None
    if not article.external_id.startswith("arxiv:"):
        return None
    identifier = article.external_id.removeprefix("arxiv:")
    if not ARXIV_BASE_ID.fullmatch(identifier):
        return None
    expected = "https://arxiv.org/abs/" + identifier
    return expected if article.url == expected else None


def cache_research_abstract(session, article: Article, config: RadarConfig):
    """Save source API evidence, never an inferred full paper or an approval.

    The collector refreshes this snapshot. Retaining exactly the article title
    and text also reuses its content-addressed translation cache.
    """
    url = research_source_url(article)
    if not url or not article.text.startswith(RESEARCH_ABSTRACT_PREFIX):
        return None
    if not article.title.strip() or not article.text[len(RESEARCH_ABSTRACT_PREFIX):].strip():
        return None
    key = fingerprint(url)
    doc = session.get(WebDocument, key)
    if doc and doc.text and (
        not doc.partial or not doc.text.startswith(RESEARCH_ABSTRACT_PREFIX)
        or doc.content_type not in {"", "text/plain"}
    ):
        return doc  # A previously obtained full/partial paper must not be downgraded to its abstract.
    if doc is None:
        doc = WebDocument(id=key, url=url)
        session.add(doc)
    content_hash = fingerprint(article.title, article.text, True)
    if doc.content_hash != content_hash:
        doc.analysis_id = ""
    doc.title, doc.text, doc.partial = article.title, article.text, True
    doc.content_hash, doc.final_url, doc.content_type = content_hash, url, "text/plain"
    doc.status, doc.message = "fetched", "仅取得官方作者摘要，未读取论文全文。"
    doc.fetched_at = now_iso()
    doc.retry_at = (datetime.now(UTC) + timedelta(hours=config.reading.refresh_hours)).isoformat()
    doc.etag, doc.modified, doc.links = "", "", []
    session.flush()
    return doc


def research_root_ids(session) -> set[str]:
    # Abstract roots are refreshed by the source API, even if a prior web fetch
    # failed or the user requests a forced processing pass.
    return {fingerprint(url) for article in session.scalars(select(Article).where(
        Article.platform == "web", Article.source_id.in_(RESEARCH_SOURCES),
    )) if (url := research_source_url(article)) is not None}


def research_reference(url: str) -> bool:
    path = urlsplit(url)
    if re.search(r"\.pdf(?:$|/)", path.path, re.I):
        return False
    if (path.hostname or "").removeprefix("www.") == "huggingface.co" and (
        path.path.rstrip("/") == "/papers" or path.path.startswith("/papers/")
    ):
        # Keep community URLs as references, but don't promote the platform's
        # generated AI summary into independently supplied paper evidence.
        return False
    return not ((path.hostname or "").removeprefix("www.") in {"arxiv.org", "export.arxiv.org"}
                and re.match(r"^/(?:abs|pdf|html|e-print|format)/", path.path, re.I))


def fingerprint(*values) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def analysis_key(session, document: WebDocument, config: RadarConfig) -> str:
    if technical_document(document.title, document.text) and config.provider.kind != 'extractive':
        return fingerprint(document.content_hash, config.reading.revision, "technical-original-summary-v1",
                           document.id, document.final_url or document.url)
    legacy = fingerprint(document.content_hash, config.reading.revision)
    existing = session.get(DocumentAnalysis, legacy)
    if document.analysis_id == legacy and existing is not None and existing.status == "ready":
        # Preserve published history; it is not retroactively labelled as
        # having passed the new independent source review.
        return legacy
    if not config.summary_review.enabled or config.provider.kind == "extractive":
        return legacy
    return fingerprint(document.content_hash, config.reading.revision, "source-bound-summary-v1",
                       document.id, document.final_url or document.url)


def terminology_ready(session, document: WebDocument, config: RadarConfig) -> bool:
    # Summary evidence is the original document. Translation has an independent lifecycle.
    return True


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
    research = research_source_url(article) is not None
    if article.platform in {"rss", "web"} and root_url:
        seeds.append({"url": root_url, "label": article.title[:300], "relation": "source"})
    direct = [ref for ref in (source.references if source else []) if ref.get("kind") != "reply"]
    expanded = {normalize_link(ref.get("short_url", "")) for ref in direct
                if ref.get("short_url") and ref["short_url"] != ref["url"]}
    if not research:
        direct += [ref for ref in text_references(article.text) if ref["url"] not in expanded]
    # Only the SOURCE page can contribute first-hop links. Child document links are never expanded.
    root = session.get(WebDocument, fingerprint(root_url)) if seeds else None
    if root and not research:
        direct += [ref for ref in root.links if not (
            urlsplit(ref['url']).hostname == urlsplit(root.final_url or root.url).hostname
            and urlsplit(ref['url']).path.rstrip('/') in {'', '/blog', '/news', '/index'})]
    mentions = ([] if research else
                mentioned_references(article.title + "\n" + article.text, config.reading.mention_catalog))
    if root and not research:
        mentions += mentioned_references(root.text, config.reading.mention_catalog)
    seen = {root_url} if seeds else set()
    child_count = 0
    for candidate in direct + mentions:
        url = normalize_link(candidate["url"], root_url or article.url)
        if not url or url in seen or not useful_link(url) or (research and not research_reference(url)):
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


def resource_views(session, article_ids: list[str], config, *, full=False, radar_config=None) -> dict[str, list[dict]]:
    bindings = session.execute(select(ArticleDocument, WebDocument).join(WebDocument).where(
        ArticleDocument.article_id.in_(article_ids))).all()
    analyses = {a.id: a for a in session.scalars(select(DocumentAnalysis).where(
        DocumentAnalysis.id.in_({doc.analysis_id for _, doc in bindings})))}
    translation_ids = {cache_key(doc.title, doc.text, config) for _, doc in bindings if doc.text}
    translated = {t.id: t for t in session.scalars(select(Translation).where(Translation.id.in_(translation_ids)))}
    captures = {c.document_id: c for c in session.scalars(select(DocumentCapture).where(
        DocumentCapture.document_id.in_({doc.id for _, doc in bindings})))}
    result = {uid: [] for uid in article_ids}
    seen = {uid: set() for uid in article_ids}
    for binding, doc in sorted(bindings, key=lambda row: (row[0].relation != "source", row[1].url)):
        canonical = doc.final_url or doc.url
        if canonical in seen[binding.article_id]:
            continue
        seen[binding.article_id].add(canonical)
        analysis = analyses.get(doc.analysis_id)
        if (radar_config and technical_document(doc.title, doc.text)
                and radar_config.provider.kind != "extractive"
                and doc.analysis_id != analysis_key(session, doc, radar_config)):
            analysis = None
        ready = analysis is not None and analysis.status == "ready"
        zh = translated.get(cache_key(doc.title, doc.text, config)) if config.enabled and doc.text else None
        zh_ready = zh is not None and zh.status == "ready"
        view = {
            "id": doc.id, "url": doc.url, "resolved_url": canonical, "relation": binding.relation,
            "title": doc.title or binding.label or urlsplit(doc.url).hostname,
            "title_zh": analysis.title_zh if ready else (zh.title_zh if zh_ready else None),
            "status": "ready" if ready else (
                "analysis_review_required" if analysis and analysis.status == "review_required" else
                "analysis_error" if analysis and analysis.status == "error" else
                "pending" if doc.text else doc.status),
            "fetch_status": doc.status,
            "message": ("网页解读尚未通过自动审核，处理进度已保存，无需重新授权。"
                        if analysis and analysis.status == "review_required" else doc.message),
            "partial": doc.partial,
            "fetched_at": doc.fetched_at or None,
            "summary_zh": analysis.summary_zh if ready else None,
            "key_points_zh": analysis.key_points_zh if ready else [],
            "why_it_matters_zh": analysis.why_it_matters_zh if ready else None,
            "translation_status": zh.status if zh else ("pending" if config.enabled else "disabled"),
        }
        capture = captures.get(doc.id)
        view["capture_method"] = (capture.method if capture and capture.content_hash == doc.content_hash else "server")
        if full:
            view.update(text=doc.text, text_zh=zh.text_zh if zh_ready else None)
        result[binding.article_id].append(view)
    return result


class ReadingService:
    def __init__(self, sessions, config: RadarConfig, translations):
        self.sessions, self.config, self.translations = sessions, config, translations
        self.semaphore = asyncio.Semaphore(config.reading.concurrency)
        self.summary_reviews = SummaryReviewService(sessions, config)

    def has_pending(self) -> bool:
        """Read-only recovery signal for fetching or analysis, excluding translation."""
        if not self.config.reading.enabled:
            return False
        reviewed = self.config.summary_review.enabled and self.config.provider.kind != "extractive"
        now = now_iso()
        with self.sessions() as session:
            research_roots = research_root_ids(session)
            documents = session.scalars(select(WebDocument).join(ArticleDocument).join(Article)).unique().all()
            for document in documents:
                if document.id not in research_roots and document.retry_at <= now:
                    return True
                if not document.text:
                    continue
                if not terminology_ready(session, document, self.config):
                    continue
                key = analysis_key(session, document, self.config)
                analysis = session.get(DocumentAnalysis, key)
                if analysis and (analysis.status == "ready" or analysis.retry_at > now):
                    continue
                source = {"id": key, "url": document.final_url or document.url, "title": document.title,
                          "text": document.text, "partial": document.partial}
                if not reviewed or self.summary_reviews.can_analyze_documents(
                    "document:" + key, [source], evidence=document_review_evidence([source]),
                ):
                    return True
        return False

    async def fetch_one(self, key: str, fetcher: PageFetcher):
        async with self.semaphore:
            with self.sessions() as session:
                if key in research_root_ids(session):
                    return  # The official source API owns this root's refresh.
                doc = session.get(WebDocument, key)
                url, etag, modified = doc.url, doc.etag, doc.modified
                version = (doc.content_hash, doc.fetched_at)
            def outdated(row):
                # A user may submit a newer phone capture while this network request waits.
                return row is None or (row.content_hash, row.fetched_at) != version
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
                    if outdated(row):
                        return
                    if parsed is not None:
                        row.title = parsed["title"] or urlsplit(final).hostname or "网页正文"
                        row.text, row.links, row.partial = parsed["text"], parsed["links"], parsed["partial"]
                        content_hash = fingerprint(row.title, row.text, row.partial)
                        if content_hash != row.content_hash:
                            row.analysis_id = ""
                        row.content_hash = content_hash
                        row.final_url, row.content_type = final, response_headers.get("content-type", "")
                        row.etag, row.modified = response_headers.get("etag", ""), response_headers.get("last-modified", "")
                    if not row.text:
                        raise ValueError("304 without cached content")
                    row.status, row.message, row.fetched_at = "fetched", "", now_iso()
                    row.retry_at = (datetime.now(UTC) + timedelta(hours=self.config.reading.refresh_hours)).isoformat()
            except (PageUnavailable, httpx.HTTPError, OSError, ValueError, TimeoutError) as exc:
                from .browser_access import browser_fallback, save_capture
                with self.sessions() as session:
                    if outdated(session.get(WebDocument, key)):
                        return
                if not isinstance(exc, PageUnavailable) or exc.status not in {"blocked", "restricted", "too_large", "rate_limited"}:
                    try:
                        # A failed robots lookup is not permission to switch fetching methods.
                        await fetcher.check_robots(url)
                        result = await browser_fallback(self.sessions, url)
                        if result:
                            with self.sessions.begin() as session:
                                row = session.get(WebDocument, key)
                                if not outdated(row):
                                    save_capture(session, row, result, self.config.reading.refresh_hours)
                            return
                    except (PageUnavailable, ValueError) as browser_error:
                        exc = browser_error
                with self.sessions.begin() as session:
                    row = session.get(WebDocument, key)
                    if outdated(row):
                        return
                    row.status = exc.status if isinstance(exc, PageUnavailable) else "unavailable"
                    row.message = exc.message if isinstance(exc, PageUnavailable) else "暂未取得可阅读正文，可能为失效链接、扫描件或动态页面。"
                    row.retry_at = (datetime.now(UTC) + timedelta(hours=6)).isoformat()

    async def pending(self, *, force=False, translate=True, limit: int | None = None) -> dict:
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("网页处理数量必须为正整数。")
        if not self.config.reading.enabled:
            return {"enabled": False}
        max_documents = min(limit, self.config.reading.max_documents) if limit is not None \
            else self.config.reading.max_documents
        reviewed = self.config.summary_review.enabled and self.config.provider.kind != "extractive"
        source_documents = {}
        with self.sessions.begin() as session:
            articles = session.scalars(select(Article).order_by(Article.published_at.desc())).all()
            for article in articles:
                sync_documents(session, article, self.config)
            ids = [a.id for a in articles]
        fetcher = PageFetcher()
        fetched = set()
        # Two fixed phases: source/explicit targets first, source-body references second. No traversal loop.
        for _phase in range(2):
            remaining = max_documents - len(fetched)
            if remaining <= 0:
                break
            with self.sessions.begin() as session:
                for article in session.scalars(select(Article).where(Article.id.in_(ids))):
                    sync_documents(session, article, self.config)
                query = select(WebDocument).join(ArticleDocument).join(Article).where(
                    Article.id.in_(ids)).order_by(Article.published_at.desc(), ArticleDocument.relation != "source")
                candidates = session.scalars(query).all()
                research_roots = research_root_ids(session)
                keys = list(dict.fromkeys(d.id for d in candidates if d.id not in fetched
                                         and d.id not in research_roots
                                         and (d.retry_at <= now_iso() or (force and not d.text))))[:remaining]
            fetched.update(keys)
            await asyncio.gather(*(self.fetch_one(key, fetcher) for key in keys))
        with self.sessions.begin() as session:
            for article in session.scalars(select(Article).where(Article.id.in_(ids))):
                sync_documents(session, article, self.config)
            docs = session.scalars(select(WebDocument).join(ArticleDocument).join(Article).where(
                WebDocument.text != "").order_by(Article.published_at.desc())).unique().all()
            payloads, seen = [], set()
            selected_documents = set(fetched)
            for doc in docs:
                key = analysis_key(session, doc, self.config)
                doc.analysis_id = key
                analysis = session.get(DocumentAnalysis, key)
                if not analysis:
                    analysis = DocumentAnalysis(id=key)
                    session.add(analysis)
                    session.flush()
                # Saved original evidence can be summarized without another network fetch.
                if key in seen:
                    continue
                seen.add(key)
                needs_analysis = analysis.status != "ready" and (force or analysis.retry_at <= now_iso())
                source = {"id": key, "url": doc.final_url or doc.url, "title": doc.title,
                          "text": doc.text, "partial": doc.partial}
                if needs_analysis and reviewed:
                    needs_analysis = self.summary_reviews.can_analyze_documents(
                        "document:" + key, [source], evidence=document_review_evidence([source]),
                    )
                if not needs_analysis:
                    continue
                if limit is not None and doc.id not in selected_documents and len(selected_documents) >= max_documents:
                    continue
                selected_documents.add(doc.id)
                source_documents[key] = doc.id
                payloads.append(dict(source, needs_analysis=needs_analysis))
        payloads = payloads[:max_documents]
        # Never feed a changing translation into summary generation or its cache identity.
        to_analyze = [{k: v for k, v in doc.items() if k != "needs_analysis"}
                      for doc in payloads if doc["needs_analysis"]]
        completed = 0
        provider = make_provider(self.config.provider)
        batch_size = 1 if reviewed else 4
        for offset in range(0, len(to_analyze), batch_size):
            batch = to_analyze[offset:offset + batch_size]
            try:
                if reviewed:
                    evidence = document_review_evidence(batch)
                    output = await self.summary_reviews.analyze_documents(
                        "document:" + batch[0]["id"], batch, provider, evidence=evidence,
                    )
                else:
                    output = await provider.analyze(batch)
                with self.sessions.begin() as session:
                    if reviewed:
                        reserve_publication(session)
                        doc = session.get(WebDocument, source_documents[batch[0]["id"]])
                        if (doc is None or doc.analysis_id != batch[0]["id"]
                                or analysis_key(session, doc, self.config) != doc.analysis_id):
                            raise SummaryReviewPending("摘要来源已更新，等待服务器使用新证据审核。")
                        current = document_review_evidence([{
                            "id": doc.analysis_id, "url": doc.final_url or doc.url,
                            "title": doc.title, "text": doc.text, "partial": doc.partial,
                        }])
                        if review_evidence_fingerprint(current) != review_evidence_fingerprint(evidence):
                            raise SummaryReviewPending("摘要来源已更新，等待服务器使用新证据审核。")
                    for summary in output.documents:
                        row = session.get(DocumentAnalysis, summary.source_id)
                        if row is None or row.status == "ready":
                            continue
                        for name, value in summary.model_dump(exclude={"source_id"}).items():
                            setattr(row, name, value)
                        row.status, row.provider, row.model = "ready", self.config.provider.kind, self.config.provider.model or ""
                        row.updated_at, row.retry_at = now_iso(), ""
                        completed += 1
            except Exception as exc:
                with self.sessions.begin() as session:
                    for doc in batch:
                        row = session.get(DocumentAnalysis, doc["id"])
                        if row is None or row.status == "ready":
                            continue
                        row.status = ("pending" if isinstance(exc, SummaryReviewYield) else
                                      "review_required" if isinstance(exc, SummaryReviewPending) else "error")
                        row.retry_at = ("" if isinstance(exc, SummaryReviewYield) else
                                        (datetime.now(UTC) + timedelta(minutes=30)).isoformat())
                        row.updated_at = now_iso()
        return {"enabled": True, "fetched": len(fetched), "summarized": completed}

    def evidence(self, articles: list[dict]) -> list[dict]:
        with self.sessions() as session:
            resources = resource_views(session, [a["id"] for a in articles], self.config.translation,
                                       radar_config=self.config)
        return [dict(a, evidence_type="source_excerpt", resources=[
            {k: v for k, v in resource.items() if k in {
                "url", "resolved_url", "relation", "title", "title_zh", "summary_zh", "key_points_zh",
                "why_it_matters_zh", "partial", "fetched_at"}}
            for resource in resources[a["id"]] if resource["status"] == "ready"
        ]) for a in articles]
