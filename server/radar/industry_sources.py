"""Bounded, credential-free primary evidence for personal industry research.

Only the static registry is fetchable. Feed descriptions and policy abstracts
remain excerpts; SEC notice metadata is never presented as filing prose.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urlencode, urlsplit, urlunsplit

import feedparser
import httpx
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class SourceSpec:
    id: str
    name: str
    url: str
    kind: str
    entity_ids: tuple[str, ...]
    description: str
    limitations: str
    refresh_minutes: int
    fee: str = "free"


@dataclass
class EvidenceInput:
    source_id: str
    external_id: str
    url: str
    title: str
    text: str
    published_at: str
    published_precision: Literal["timestamp", "date"]
    kind: str
    entity_ids: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class IndustrySourceError(Exception):
    """Sanitized collection failure; no upstream body, headers or credentials."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.status = code
        self.message = message
        super().__init__(message)


# CIKs identify issuers, while ticker/exchange identify the watched share listing.
_ENTITY_ROWS = (
    ("nvidia", "NVIDIA", "NVDA", "NASDAQ", "0001045810", ["infrastructure"]),
    ("amd", "Advanced Micro Devices", "AMD", "NASDAQ", "0000002488", ["infrastructure"]),
    ("broadcom", "Broadcom", "AVGO", "NASDAQ", "0001730168", ["infrastructure", "software"]),
    ("micron", "Micron Technology", "MU", "NASDAQ", "0000723125", ["infrastructure"]),
    ("tsmc", "Taiwan Semiconductor Manufacturing", "TSM", "NYSE", "0001046179", ["infrastructure"]),
    ("asml", "ASML", "ASML", "NASDAQ", "0000937966", ["infrastructure"]),
    ("arm", "Arm Holdings", "ARM", "NASDAQ", "0001973239", ["infrastructure"]),
    ("vertiv", "Vertiv", "VRT", "NYSE", "0001674101", ["infrastructure"]),
    ("eaton", "Eaton", "ETN", "NYSE", "0001551182", ["infrastructure"]),
    ("schneider", "Schneider Electric", "SU", "EURONEXT_PARIS", None, ["infrastructure"]),
    ("microsoft", "Microsoft", "MSFT", "NASDAQ", "0000789019", ["infrastructure", "software", "workflows"]),
    ("amazon", "Amazon", "AMZN", "NASDAQ", "0001018724", ["infrastructure", "software", "workflows"]),
    ("alphabet", "Alphabet", "GOOGL", "NASDAQ", "0001652044", ["infrastructure", "software", "workflows"]),
    ("meta", "Meta Platforms", "META", "NASDAQ", "0001326801", ["infrastructure", "workflows"]),
    ("oracle", "Oracle", "ORCL", "NYSE", "0001341439", ["infrastructure", "software"]),
    ("salesforce", "Salesforce", "CRM", "NYSE", "0001108524", ["software", "workflows"]),
    ("servicenow", "ServiceNow", "NOW", "NYSE", "0001373715", ["software", "workflows"]),
    ("adobe", "Adobe", "ADBE", "NASDAQ", "0000796343", ["software", "workflows"]),
    ("sap", "SAP", "SAP", "NYSE", "0001000184", ["software", "workflows"]),
    ("palantir", "Palantir Technologies", "PLTR", "NASDAQ", "0001321655", ["software", "workflows"]),
    ("intuit", "Intuit", "INTU", "NASDAQ", "0000896878", ["software", "workflows"]),
    ("adp", "Automatic Data Processing", "ADP", "NASDAQ", "0000008670", ["workflows"]),
    ("accenture", "Accenture", "ACN", "NYSE", "0001467373", ["workflows"]),
    ("ibm", "International Business Machines", "IBM", "NYSE", "0000051143", ["software", "workflows"]),
    ("autodesk", "Autodesk", "ADSK", "NASDAQ", "0000769397", ["software", "workflows"]),
)
ENTITIES = {
    row[0]: {"id": row[0], "name": row[1], "ticker": row[2], "exchange": row[3],
             "cik": row[4], "themes": row[5]}
    for row in _ENTITY_ROWS
}
_RSS_LIMIT = "仅当前 RSS 窗口，无完整历史或秒级 SLA；摘要不等于全文；企业表述需交叉核实。"
SOURCES: dict[str, SourceSpec] = {
    "fed-monetary": SourceSpec(
        "fed-monetary", "Federal Reserve 货币政策", "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "macro_policy", (), "货币政策声明与会议纪要，作为融资成本和估值背景。",
        "仅 RSS 标题与摘要；宏观背景不直接证明 AI 行业因果。", 30),
    "federal-register-ai": SourceSpec(
        "federal-register-ai", "Federal Register AI / 芯片政策",
        "https://www.federalregister.gov/api/v1/documents.json", "policy", (),
        "正式政策元数据和摘要；本地再次筛选 AI、半导体、数据中心主题。",
        "只读最近最多 2 页，可能截断；publication_date 仅日期；摘要不等于法条全文。", 60),
    "nvidia-news": SourceSpec(
        "nvidia-news", "NVIDIA 官方新闻", "https://nvidianews.nvidia.com/cats/press_release.xml",
        "company_release", ("nvidia",), "产品、合作、基础设施和商业进展。",
        _RSS_LIMIT + " NVIDIA RSS 官方条款限个人非商业用途。", 30),
    "amd-ir": SourceSpec(
        "amd-ir", "AMD 投资者关系", "https://ir.amd.com/news-events/press-releases/rss",
        "company_release", ("amd",), "财报、产品和客户合作公告。", _RSS_LIMIT, 30),
    "broadcom-ir": SourceSpec(
        "broadcom-ir", "Broadcom 投资者关系", "https://investors.broadcom.com/rss/news-releases.xml",
        "company_release", ("broadcom",), "财报、定制芯片、网络与企业软件公告。", _RSS_LIMIT, 30),
}
# A bounded initial watch list, not a claim of whole-market coverage.
_SEC_ENTITIES = (
    "nvidia", "amd", "broadcom", "micron", "microsoft", "amazon", "alphabet", "meta",
    "oracle", "salesforce", "servicenow", "adobe", "palantir", "intuit", "adp", "accenture", "ibm",
)
for _entity in _SEC_ENTITIES:
    _cik = ENTITIES[_entity]["cik"]
    SOURCES[f"sec-{_entity}"] = SourceSpec(
        f"sec-{_entity}", f"SEC · {ENTITIES[_entity]['name']}",
        f"https://data.sec.gov/submissions/CIK{_cik}.json", "filing", (_entity,),
        "SEC 最近申报元数据；最多尝试最新 1 份经营/定期申报主文档。",
        "最近 submissions 窗口；notice 只有元数据。主文档不包含所有附件；不可用时保留 notice。"
        "进程内 SEC 请求串行并间隔至少 250ms；多进程仍需外部总限频。", 60)

MAX_RESPONSE_BYTES = 2_000_000
MAX_TEXT_CHARS = 60_000
MAX_ENTRY_CHARS = 400_000
MAX_ROWS = 200
MAX_ITEMS = 30
MAX_POLICY_PAGES = 2
_SEC_LOCK = asyncio.Lock()
_SEC_NEXT = 0.0
_POLICY_TERMS = re.compile(
    r"\bartificial intelligence\b|\bgenerative (?:ai|artificial intelligence)\b|"
    r"\bmachine learning\b|\bdata[ -]cent(?:er|re)s?\b|\bsemiconductors?\b|"
    r"\badvanced[ -]computing\b|\bgraphics processing units?\b|"
    r"\bAI[ -](?:models?|systems?|governance|safety|agents?|development|infrastructure)\b|"
    r"\b(?:computer|computing|advanced|AI)[ -]chips?\b", re.I)
_ALLOWED_FORMS = {"8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "6-K"}


def _error(source: SourceSpec, code: str, reason: str) -> IndustrySourceError:
    return IndustrySourceError(code, f"{source.id}: {reason}")


def _safe_url(value: str, hosts: set[str]) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid URL")
    p = urlsplit(value)
    if (p.scheme != "https" or p.hostname not in hosts or p.username or p.password
            or p.port not in (None, 443) or "\\" in value):
        raise ValueError("Unexpected publisher URL")
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def _plain(value: object, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", False
    clipped = len(value) > MAX_ENTRY_CHARS
    soup = BeautifulSoup(value[:MAX_ENTRY_CHARS], "html.parser")
    for node in soup.select("script,style,noscript,template,svg,ix\\:hidden"):
        node.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    return text[:limit], clipped or len(text) > limit


def _date(value: object) -> tuple[datetime, Literal["timestamp", "date"]]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing publisher date")
    raw = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return datetime.fromisoformat(raw).replace(tzinfo=UTC), "date"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        dt = parsedate_to_datetime(raw)
    if dt.tzinfo is None:
        raise ValueError("Ambiguous timezone")
    return dt.astimezone(UTC), "timestamp"


def _current(dt: datetime, now: datetime, days: int) -> bool:
    # Never infer publication from retrieval, updated, event, or fiscal dates.
    return now - timedelta(days=days) <= dt <= now


def _input(source, external_id, url, title, text, dt, precision, kind, metadata):
    return EvidenceInput(
        source_id=source.id, external_id=str(external_id)[:500], url=url, title=title[:500], text=text,
        published_at=dt.isoformat().replace("+00:00", "Z"), published_precision=precision,
        kind=kind, entity_ids=list(source.entity_ids),
        metadata={"source_name": source.name, "fee": source.fee, **metadata})


async def _request(client: httpx.AsyncClient, source: SourceSpec, url: str, *, json_body=False) -> bytes:
    _safe_url(url, {"data.sec.gov", "www.sec.gov"} if source.kind == "filing"
              else {urlsplit(source.url).hostname})
    async def read():
        # Direct Request bypasses a supplied client's auth headers and cookie jar.
        request = httpx.Request("GET", url, headers={
            "User-Agent": "AI-Radar-Industry-Research/1.0 (personal public-source research)",
            "Accept": "application/json" if json_body else "application/rss+xml,application/xml,text/xml,text/html",
            "Accept-Encoding": "identity",
        })
        response = await client.send(request, stream=True, auth=None, follow_redirects=False)
        try:
            if response.status_code in (401, 403):
                raise _error(source, "blocked", "publisher access restricted")
            if response.status_code == 429:
                raise _error(source, "rate_limited", "publisher rate limit")
            if response.status_code != 200:
                raise _error(source, "upstream_error", f"HTTP {response.status_code}")
            ct = response.headers.get("content-type", "").lower()
            if not any(t in ct for t in (("json",) if json_body else ("xml", "html", "text/plain"))):
                raise _error(source, "invalid_response", "unexpected content type")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                raise _error(source, "too_large", "response exceeds size limit")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise _error(source, "too_large", "response exceeds size limit")
                body.extend(chunk)
            return bytes(body)
        finally:
            await response.aclose()

    async with asyncio.timeout(25):
        if source.kind != "filing":
            return await read()
        global _SEC_NEXT
        async with _SEC_LOCK:
            await asyncio.sleep(max(0, _SEC_NEXT - time.monotonic()))
            try:
                return await read()
            finally:
                _SEC_NEXT = time.monotonic() + 0.25


def _rss(source, body, now, days, limit):
    # Reject DTD/entity declarations before handing bytes to the tolerant parser.
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.I):
        raise _error(source, "invalid_response", "XML declarations not supported")
    feed = feedparser.parse(body)
    if not feed.version or not isinstance(feed.entries, list):
        raise _error(source, "invalid_response", "expected RSS or Atom feed")
    rows = []
    for e in feed.entries[:MAX_ROWS]:
        try:
            dt, precision = _date(e.get("published"))
            if not _current(dt, now, days):
                continue
            url = _safe_url(e.get("link"), {urlsplit(source.url).hostname})
            title, _ = _plain(e.get("title"), 500)
            content = e.get("content")
            explicit_content = isinstance(content, list) and bool(content) and isinstance(content[0], dict)
            raw = content[0].get("value") if explicit_content else ""
            if not isinstance(raw, str) or not raw.strip():
                raw, explicit_content = e.get("summary", ""), False
            text, truncated = _plain(raw)
            if not title:
                continue
            # content:encoded can itself be an excerpt. Do not infer completeness.
            scope = "feed_content" if explicit_content else "feed_summary"
            if not text:
                text, scope = title, "headline_only"
            rows.append(_input(source, e.get("id") or hashlib.sha256(url.encode()).hexdigest(),
                               url, title, text, dt, precision, source.kind,
                               {"evidence_scope": scope, "is_full_text": False, "truncated": truncated,
                                "history_complete": False, "publisher_url": url}))
        except (ValueError, TypeError, OverflowError):
            continue
    return _finish(rows, limit)


def _finish(rows, limit):
    ordered = sorted(rows, key=lambda r: (r.published_at, r.external_id), reverse=True)
    seen, result = set(), []
    for item in ordered:
        key = item.external_id
        if key not in seen:
            seen.add(key)
            result.append(item)
            if len(result) >= limit:
                break
    return result


async def _policy(client, source, now, days, limit):
    rows = []
    for page in range(1, MAX_POLICY_PAGES + 1):
        params = {"per_page": "100", "page": str(page), "order": "newest",
                  "conditions[term]": '"artificial intelligence" OR "data center" OR semiconductor OR "advanced computing"',
                  "conditions[publication_date][gte]": (now - timedelta(days=days)).date().isoformat()}
        body = await _request(client, source, source.url + "?" + urlencode(params), json_body=True)
        data = json.loads(body)
        results = data.get("results")
        if not isinstance(results, list):
            raise _error(source, "invalid_response", "policy results missing")
        for e in results[:100]:
            try:
                if not isinstance(e, dict):
                    continue
                dt, precision = _date(e.get("publication_date"))
                if not _current(dt, now, days):
                    continue
                url = _safe_url(e.get("html_url"), {"www.federalregister.gov"})
                title, _ = _plain(e.get("title"), 500)
                abstract, truncated = _plain(e.get("abstract"))
                # Query filtering upstream is not trusted. Bare 'chip' is too broad.
                if not title or not _POLICY_TERMS.search(title + " " + abstract):
                    continue
                number = e.get("document_number")
                if not isinstance(number, str) or not re.fullmatch(r"\d{4}-\d{4,6}", number):
                    continue
                metadata = {"evidence_scope": "policy_abstract" if abstract else "headline_only",
                            "is_full_text": False, "truncated": truncated, "document_number": number,
                            "document_type": str(e.get("type", ""))[:80],
                            "agencies": [str(a.get("name", ""))[:150] for a in e.get("agencies", [])[:10]
                            if isinstance(a, dict)], "local_topic_filter": True,
                            "history_complete": False, "max_pages": MAX_POLICY_PAGES}
                rows.append(_input(source, number, url, title, abstract or title, dt, precision,
                                   "policy_notice", metadata))
            except (ValueError, TypeError, OverflowError):
                continue
        # No next_page_url is followed: untrusted URLs never become requests.
        total = data.get("total_pages", 1)
        if len(rows) >= limit or not results or not isinstance(total, int) or page >= total:
            break
    return _finish(rows, limit)


async def _sec(client, source, now, days, limit):
    data = json.loads(await _request(client, source, source.url, json_body=True))
    entity = ENTITIES[source.entity_ids[0]]
    cik = entity["cik"]
    if str(data.get("cik", "")).lstrip("0") != cik.lstrip("0"):
        raise _error(source, "invalid_response", "issuer identity mismatch")
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form")
    if not isinstance(forms, list):
        raise _error(source, "invalid_response", "filing index missing")
    rows = []
    for i, form in enumerate(forms[:MAX_ROWS]):
        if form not in _ALLOWED_FORMS:
            continue
        try:
            def value(name, index=i):
                vals = recent.get(name, [])
                return vals[index] if isinstance(vals, list) and index < len(vals) else None
            # If acceptance time is absent, preserve the filing date's precision.
            dt, precision = _date(value("acceptanceDateTime") or value("filingDate"))
            if not _current(dt, now, days):
                continue
            accession, document = value("accessionNumber"), value("primaryDocument")
            if not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
                continue
            if not isinstance(document, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.(?:htm|html|txt)", document):
                continue
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}"
            title = f"{entity['name']} · {form} · {value('filingDate') or dt.date().isoformat()}"
            # Explicit metadata statement, not synthetic document contents.
            notice = f"SEC filing notice: {entity['name']}; form {form}; accession {accession}."
            metadata = {"evidence_scope": "filing_metadata", "is_full_text": False,
                        "issuer_cik": cik, "accession_number": accession, "form": form,
                        "filing_date": value("filingDate"), "document_fetch": "not_attempted",
                        "history_complete": False}
            rows.append(_input(source, accession, url, title, notice, dt, precision,
                               "filing_notice", metadata))
        except (ValueError, TypeError, OverflowError):
            continue
    rows = _finish(rows, limit)
    if rows:
        # One document per source per refresh bounds download work. SEC may block
        # Archives independently of data.sec.gov; the notice remains useful.
        item = rows[0]
        try:
            body = await _request(client, source, item.url)
            text, truncated = _plain(body.decode("utf-8", errors="replace"))
            if len(text) < 100 or re.search(r"undeclared automated tool|request rate threshold exceeded", text, re.I):
                raise _error(source, "invalid_response", "filing document unavailable")
            # An 8-K cover referring to an earnings exhibit is not earnings prose.
            if item.metadata["form"].startswith("8-K") and (
                    len(text) < 6000 and re.search(r"exhibit(?:s)?\s*(?:99|index)|furnished as exhibit", text, re.I)):
                item.metadata.update(document_fetch="cover_only", partial=True, includes_exhibits=False)
                return rows
            item.text, item.kind = text, "filing_document"
            item.metadata.update(evidence_scope="filing_primary_document", is_full_text=not truncated,
                                 truncated=truncated, document_fetch="available", includes_exhibits=False, partial=truncated)
        except (IndustrySourceError, httpx.HTTPError, TimeoutError, ValueError) as exc:
            item.metadata.update(document_fetch="unavailable", partial=True,
                                 document_error_code=exc.code if isinstance(exc, IndustrySourceError)
                                 else "network_or_document_error")
    return rows


_ARTICLE_SELECTORS = {
    "nvidia-news": ("/news/", ".article-body"),
    "amd-ir": ("/news-events/press-releases/detail/", "article"),
    "broadcom-ir": ("/news-releases/news-release-details/", ".xn-content"),
}


async def _enrich_rss(client, source, rows):
    """Read at most three canonical company articles; never crawl their links."""
    if source.id not in _ARTICLE_SELECTORS:
        return rows
    prefix, selector = _ARTICLE_SELECTORS[source.id]
    for item in rows[:3]:
        if not urlsplit(item.url).path.startswith(prefix):
            item.metadata["article_fetch"] = "unavailable"
            continue
        try:
            body = await _request(client, source, item.url)
            soup = BeautifulSoup(body, "html.parser")
            article = soup.select_one(selector)
            if article is None:
                raise ValueError("Publisher article container unavailable")
            text, truncated = _plain(str(article))
            if len(text) < 200:
                raise ValueError("Publisher article body unavailable")
            item.text = text
            item.metadata.update(evidence_scope="publisher_article", is_full_text=not truncated,
                                 truncated=truncated, article_fetch="available", publisher_claim=True,
                                 partial=truncated)
        except (IndustrySourceError, httpx.HTTPError, TimeoutError, ValueError) as exc:
            item.metadata.update(article_fetch="unavailable", partial=True,
                                 article_error_code=exc.code if isinstance(exc, IndustrySourceError)
                                 else "network_or_document_error")
    return rows


async def fetch_source(client: httpx.AsyncClient, source: SourceSpec, *,
                       lookback_days: int = 90, max_items: int = 12) -> list[EvidenceInput]:
    """Fetch one allowlisted source. Empty success means no eligible recent rows.

    Caller schedules refresh_minutes; this module never installs a scheduler.
    It requires no key, ignores client auth/cookies and never follows redirects.
    """
    if source.id not in SOURCES or source != SOURCES[source.id] or source.fee != "free":
        raise IndustrySourceError("invalid_source", "Only registered free sources are supported")
    if (type(lookback_days) is not int or not 1 <= lookback_days <= 365
            or type(max_items) is not int or not 1 <= max_items <= MAX_ITEMS):
        raise IndustrySourceError("invalid_options", "Invalid bounded collection options")
    now = datetime.now(UTC)
    try:
        if source.id == "federal-register-ai":
            return await _policy(client, source, now, lookback_days, max_items)
        if source.kind == "filing":
            return await _sec(client, source, now, lookback_days, max_items)
        rows = _rss(source, await _request(client, source, source.url), now, lookback_days, max_items)
        return await _enrich_rss(client, source, rows)
    except IndustrySourceError:
        raise
    except (httpx.HTTPError, TimeoutError):
        raise _error(source, "network_error", "request failed or timed out") from None
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise _error(source, "invalid_response", "publisher response could not be parsed") from None
