"""Browser-worker client and durable access state; never stores site cookies in the API."""
import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import httpx
from sqlalchemy import select, update

from .config import Settings
from .models import ArticleDocument, WebDocument, WebsiteAccess, now_iso
from .reading import fingerprint
from .web_reader import PageUnavailable

INTERVENTION_STATUSES = {"auth_required", "access_restricted"}
ACCESS_RETRY_HOURS = 6
_fallback_locks = WeakKeyDictionary()


def browser_paused(access):
    # A failed interactive attempt also has enabled=False. Only a disabled, ready
    # profile (or an explicit paused state) means the user requested a pause.
    return bool(access and (access.status == "paused" or (access.status == "ready" and not access.enabled)))


def access_retry_at(access):
    """A remembered server restriction is a cooldown, not a permanent login requirement."""
    if not access or access.status != "access_restricted":
        return None
    try:
        updated = datetime.fromisoformat(access.updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        return updated.astimezone(UTC) + timedelta(hours=ACCESS_RETRY_HOURS)
    except (TypeError, ValueError):
        # A legacy invalid timestamp gets one new, durably reserved attempt.
        return None


class BrowserClient:
    def __init__(self, settings=None):
        self.settings = settings or Settings()

    async def request(self, method, path, data=None):
        if not self.settings.browser_worker_token:
            raise PageUnavailable("disabled", "网页授权服务尚未启用。")
        async with httpx.AsyncClient(base_url=self.settings.browser_worker_url, timeout=85,
                                     trust_env=False) as client:
            try:
                response = await client.request(method, path, json=data,
                    headers={"Authorization": "Bearer " + self.settings.browser_worker_token})
                if response.status_code == 409:
                    raise PageUnavailable("busy", "另一个网站正在授权，请完成或关闭后再试。")
                if response.status_code == 410:
                    raise PageUnavailable("expired", "授权窗口已过期，请重新打开。")
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError):
                raise PageUnavailable("unavailable", "网页授权服务暂时不可用，请稍后重试。") from None


def save_capture(session, doc, result, refresh_hours=24):
    if result["status"] != "fetched":
        raise PageUnavailable(result["status"], result.get("message", "暂未取得正文。"))
    parsed = result["document"]
    if not isinstance(parsed.get("text"), str) or not 80 <= len(parsed["text"]) <= 60000:
        raise ValueError("Invalid browser document")
    doc.title = parsed["title"] or urlsplit(doc.url).hostname
    doc.text, doc.links, doc.partial = parsed["text"], parsed["links"][:30], bool(parsed["partial"])
    content_hash = fingerprint(doc.title, doc.text, doc.partial)
    if content_hash != doc.content_hash:
        doc.analysis_id = ""
    doc.content_hash = content_hash
    doc.final_url, doc.content_type = result["url"], "text/html"
    doc.status, doc.message, doc.fetched_at = "fetched", "", now_iso()
    doc.etag = doc.modified = ""
    doc.retry_at = (datetime.now(UTC) + timedelta(hours=refresh_hours)).isoformat()


async def browser_fallback(sessions, url, client=None):
    domain = urlsplit(url).hostname
    # Concurrent article reads must recheck the domain after a previous attempt
    # discovers a challenge. Never launch one challenge window per article.
    locks = _fallback_locks.setdefault(sessions, {})
    async with locks.setdefault(domain, asyncio.Lock()):
        probe_reserved_at = ""
        with sessions.begin() as session:
            access = session.get(WebsiteAccess, domain)
            if browser_paused(access) or (access and access.status == "auth_required"):
                return None
            retry_at = access_retry_at(access)
            if retry_at and retry_at > datetime.now(UTC):
                return None
            if access and access.status == "access_restricted":
                # Reserve before I/O: cancellation, worker failure or another API
                # process must not let every article probe the same restricted site.
                probe_reserved_at = now_iso()
                claim = session.execute(update(WebsiteAccess).where(
                    WebsiteAccess.domain == domain,
                    WebsiteAccess.status == "access_restricted",
                    WebsiteAccess.updated_at == access.updated_at,
                ).values(updated_at=probe_reserved_at), execution_options={"synchronize_session": False})
                if claim.rowcount != 1:
                    return None
        result = await (client or BrowserClient()).request("POST", "/fetch", {"url": url})
        with sessions.begin() as session:
            access = session.get(WebsiteAccess, domain)
            if not access:
                access = WebsiteAccess(domain=domain)
                session.add(access)
            stale_probe = probe_reserved_at and (
                access.status != "access_restricted" or access.updated_at != probe_reserved_at)
            if not browser_paused(access) and not stale_probe and result["status"] not in {
                    "blocked", "restricted", "too_large", "rate_limited"}:
                # A transient worker error is not evidence that a previous site
                # restriction disappeared. Keep the per-domain cooldown in effect.
                status = result["status"]
                if probe_reserved_at and status not in {"fetched", "auth_required"}:
                    status = "access_restricted"
                access.status = "ready" if status == "fetched" else status
                if result["status"] == "fetched":
                    access.enabled = True
                    access.message = "已自动读取公开网页，无需手动授权。"
                else:
                    access.message = result.get("message", "浏览器暂时无法读取正文。")
                access.updated_at = now_iso()
        if result["status"] != "fetched":
            raise PageUnavailable(result["status"], result.get("message", "浏览器暂时无法读取正文。"))
        return result


def sites(session):
    now = datetime.now(UTC)
    states = {row.domain: row for row in session.scalars(select(WebsiteAccess))}
    grouped = {}
    docs = session.scalars(select(WebDocument).join(ArticleDocument)).unique().all()
    for doc in docs:
        domain = urlsplit(doc.url).hostname
        if not domain:
            continue
        group = grouped.setdefault(domain, {"domain": domain, "total": 0, "pending": 0, "fetched": 0,
                                           "document_id": doc.id, "url": doc.url, "statuses": {}, "_missing": []})
        group["total"] += 1
        group["statuses"][doc.status] = group["statuses"].get(doc.status, 0) + 1
        if not doc.text:
            group["pending"] += 1
            group["_missing"].append(doc)
        else:
            group["fetched"] += 1
    for domain, group in grouped.items():
        state = states.get(domain)
        domain_retry = access_retry_at(state)
        missing = group.pop("_missing")
        group.update(enabled=bool(state and state.enabled), access_status=state.status if state else "unverified",
                     browser_enabled=not browser_paused(state) and not (
                         state and state.status == "auth_required") and not (domain_retry and domain_retry > now),
                     verified_at=state.verified_at if state else "", retry_at="")
        counts = {"fetched": group["fetched"], "automatic": 0, "retry": 0, "login": 0, "restricted": 0}
        choices = []
        for doc in missing:
            status = doc.status
            # A remembered browser challenge is stronger evidence than an older
            # generic HTTP/parse failure, but never overrides a document's robots
            # prohibition, rate limit or size limit.
            if status not in {"blocked", "restricted", "rate_limited", "too_large"} and state and (
                    state.status in INTERVENTION_STATUSES):
                status = state.status
            if status in {"blocked", "restricted", "too_large"}:
                action = "restricted"
            elif status == "auth_required":
                action = "login"
            elif status in {"unavailable", "unsupported", "rate_limited", "busy", "disabled", "access_restricted"}:
                action = "retry"
            else:
                status, action = "pending", "automatic"
            counts[action] += 1
            choices.append(({"login": 4, "restricted": 3, "retry": 2, "automatic": 1}[action], doc, status, action))
        group["counts"] = counts
        if not missing:
            group.update(status="fetched", action="none", automatic=False,
                         message=f"已取得全部 {group['fetched']} 篇正文，无需授权；按计划检查更新。")
            continue
        _, target, status, action = max(choices, key=lambda choice: choice[0])
        group.update(document_id=target.id, url=target.url, status=status, action=action,
                     automatic=action in {"automatic", "retry"}, retry_at=target.retry_at)
        if status == "access_restricted" and domain_retry:
            # The document queue and domain cooldown must both be due. Normalize
            # timestamps before comparison because callers may use UTC offsets.
            try:
                document_retry = datetime.fromisoformat(target.retry_at)
                if document_retry.tzinfo is None:
                    document_retry = document_retry.replace(tzinfo=UTC)
                domain_retry = max(domain_retry, document_retry)
            except (TypeError, ValueError):
                pass
            group["retry_at"] = domain_retry.astimezone(UTC).isoformat()
        messages = {
            "pending": "等待自动读取，无需逐个点击采集。",
            "unavailable": "读取暂时失败，将按计划自动重试。",
            "unsupported": "普通读取未取得正文，将自动尝试浏览器读取。",
            "busy": "浏览器正由用户操作，稍后自动重试。",
            "disabled": "浏览器读取服务暂不可用，公开网页仍按计划重试。",
            "rate_limited": "网站限制访问频率，将在冷却后自动重试。",
            "auth_required": "网站要求登录或访问验证，可在手机打开目标文章处理。",
            "access_restricted": "网站暂时限制服务器访问，系统每六小时最多自动检查一次；无需逐篇操作，也可选择手机读取。",
            "restricted": "网站规则不允许自动读取该页面。",
            "blocked": "该链接不属于允许读取的公开网页。",
            "too_large": "页面超过读取大小限制，无需登录授权。",
        }
        group["message"] = messages.get(status, "等待自动处理。")
        if browser_paused(state):
            group.update(status="paused", action="none", automatic=False, browser_enabled=False,
                         message="浏览器补采已暂停；公开网页仍按计划读取。")
    return sorted(grouped.values(), key=lambda g: (-g["pending"], g["domain"]))
