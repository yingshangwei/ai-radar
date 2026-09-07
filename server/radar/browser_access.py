"""Browser-worker client and durable access state; never stores site cookies in the API."""
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from .config import Settings
from .models import ArticleDocument, WebDocument, WebsiteAccess, now_iso
from .reading import fingerprint
from .web_reader import PageUnavailable


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
    doc.content_hash = fingerprint(doc.title, doc.text, doc.partial)
    doc.final_url, doc.content_type = result["url"], "text/html"
    doc.status, doc.message, doc.fetched_at = "fetched", "", now_iso()
    doc.etag = doc.modified = ""
    doc.retry_at = (datetime.now(UTC) + timedelta(hours=refresh_hours)).isoformat()


async def browser_fallback(sessions, url, client=None):
    domain = urlsplit(url).hostname
    with sessions() as session:
        access = session.get(WebsiteAccess, domain)
        if not access or not access.enabled or access.status != "ready":
            return None
    result = await (client or BrowserClient()).request("POST", "/fetch", {"url": url})
    if result["status"] != "fetched":
        with sessions.begin() as session:
            access = session.get(WebsiteAccess, domain)
            if result["status"] in {"auth_required", "access_restricted"}:
                access.status = result["status"]
                access.message = result.get("message", "请在授权中心重新验证。")
                access.updated_at = now_iso()
        raise PageUnavailable(result["status"], result.get("message", "浏览器暂时无法读取正文。"))
    return result


def sites(session):
    states = {row.domain: row for row in session.scalars(select(WebsiteAccess))}
    grouped = {}
    docs = session.scalars(select(WebDocument).join(ArticleDocument)).unique().all()
    for doc in docs:
        domain = urlsplit(doc.url).hostname
        if not domain:
            continue
        group = grouped.setdefault(domain, {"domain": domain, "total": 0, "pending": 0,
                                           "document_id": doc.id, "url": doc.url, "statuses": {}})
        group["total"] += 1
        group["statuses"][doc.status] = group["statuses"].get(doc.status, 0) + 1
        if not doc.text:
            group["pending"] += 1
            if doc.status in {"auth_required", "access_restricted", "unavailable", "unsupported"}:
                group["document_id"], group["url"] = doc.id, doc.url
    for domain, group in grouped.items():
        state = states.get(domain)
        group.update(enabled=bool(state and state.enabled), status=state.status if state else "unverified",
                     message=state.message if state else "可打开浏览器检查目标文章。",
                     verified_at=state.verified_at if state else "")
    return sorted(grouped.values(), key=lambda g: (-g["pending"], g["domain"]))
