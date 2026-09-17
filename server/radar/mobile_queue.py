"""Bounded, admin-only work for sites explicitly permitted on the current phone.

The phone owns its permissions, foreground execution, challenge pause and retry
backoff. A server-browser challenge is not evidence that the phone is blocked.
"""
import ipaddress
import re
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Query
from sqlalchemy import func, select

from .links import normalize_link
from .models import Article, ArticleDocument, WebDocument

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_DOCUMENT_ID = re.compile(r"[a-f0-9]{64}", re.ASCII)
_EXCLUDED = {"blocked", "restricted", "rate_limited"}


def permitted_domains(value: str) -> set[str]:
    if not value:
        return set()
    values = value.split(",")
    if len(values) > 30:
        raise HTTPException(400, "一次最多检查 30 个许可网站")
    result = set()
    for raw in values:
        host = raw.strip().lower()
        if (not host or len(host) > 253 or "." not in host or host.rsplit(".", 1)[-1].isdigit() or
                not all(_LABEL.fullmatch(label) for label in host.split(".")) or
                host.endswith((".localhost", ".local", ".internal"))):
            raise HTTPException(400, "网站许可须使用完整公开域名，不能填写网址、端口或通配符")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise HTTPException(400, "网站许可须使用域名，不能填写 IP 地址")
        result.add(host.removeprefix("www."))
    return result


def excluded_documents(value: str) -> set[str]:
    if not value:
        return set()
    values = value.split(",")
    if len(values) > 100:
        raise HTTPException(400, "一次最多跳过 100 篇暂缓读取的文章")
    if not all(_DOCUMENT_ID.fullmatch(key) for key in values):
        raise HTTPException(400, "暂缓读取的文章标识格式无效")
    return set(values)


def mount_mobile_queue(router, sessions, admin):
    @router.get("/mobile-queue", dependencies=[Depends(admin)])
    async def mobile_queue(domains: str = Query(default="", max_length=8000),
                           limit: int = Query(default=3, ge=1, le=5),
                           exclude_document_ids: str = Query(default="", max_length=6500)):
        permitted = permitted_domains(domains)
        excluded = excluded_documents(exclude_document_ids)
        if not permitted:
            return []
        # Group by the durable document key before limiting: one page may be
        # referenced by several articles, and the newest association determines
        # its priority. Never enqueue unbound URLs or already acquired content.
        query = (select(WebDocument)
                 .join(ArticleDocument, ArticleDocument.document_id == WebDocument.id)
                 .join(Article, Article.id == ArticleDocument.article_id)
                 .where(WebDocument.text == "", WebDocument.status.not_in(_EXCLUDED))
                 .group_by(WebDocument.id)
                 .order_by(func.max(Article.published_at).desc(), WebDocument.id))
        if excluded:
            # Backoff is local to this phone/request; it must not modify the
            # document's server retry time or hide it from another device.
            query = query.where(WebDocument.id.not_in(excluded))
        result = []
        with sessions() as session:
            for doc in session.scalars(query).yield_per(100):
                url = normalize_link(doc.url)
                if not url:
                    continue
                domain = urlsplit(url).hostname
                if not domain or domain.removeprefix("www.") not in permitted:
                    continue
                result.append({"document_id": doc.id, "url": url, "domain": domain})
                if len(result) == limit:
                    break
        return result
