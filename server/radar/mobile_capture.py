"""Import an article explicitly confirmed on a user's device, without remote fetching."""
import asyncio
import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from .browser_access import save_capture
from .links import normalize_link, useful_link
from .models import ArticleDocument, DocumentCapture, WebDocument, now_iso
from .reading import fingerprint


class CaptureLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=4000)
    label: str = Field(default="", max_length=300)


class MobileCapture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    url: str = Field(max_length=4000)
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=80, max_length=60000)
    links: list[CaptureLink] = Field(default_factory=list, max_length=30)
    partial: bool = False
    method: Literal["mobile_browser", "mac_browser", "manual"] = "mobile_browser"


def target_identity(url):
    normalized = normalize_link(url)
    if not normalized:
        return None
    p = urlsplit(normalized)
    return p.hostname.removeprefix("www."), p.path.rstrip("/"), p.query


def validate_capture(body, doc):
    url = normalize_link(body.url)
    identity = target_identity(url or "")
    if not identity or identity not in {target_identity(doc.url), target_identity(doc.final_url)}:
        raise HTTPException(422, "当前页面不是所选文章，请返回原文；若文章正常跳转，可复制正文后手动提交。")
    if doc.status == "blocked":
        raise HTTPException(422, "该链接不属于可读取的公开网页。")
    title, text = body.title.strip(), body.text.strip()
    if len(text) < 80 or not title:
        raise HTTPException(422, "未取得足够的文章正文，请打开文章后再提交。")
    # Only reject clear login/challenge documents, not articles discussing these topics.
    if (re.match(r"^(?:just a moment|access denied|verify (?:you|that you)|sign in|log in|登录|人机验证)(?:\b|\s|[·|—-])",
                 title, re.I)
            or (len(text) < 1500 and any(marker in text.lower() for marker in (
                "verifying you are human", "verify you are human", "performing security verification",
                "enable javascript and cookies to continue", "正在验证您是否是真人")))):
        raise HTTPException(422, "提交的是登录或验证页面，尚未取得原文，请先在采集设备完成验证。")
    links = {}
    for link in body.links:
        normalized = normalize_link(link.url, url)
        if normalized and useful_link(normalized):
            links[normalized] = {"url": normalized, "label": link.label}
    return {"title": title, "text": text, "partial": body.partial or body.method == "manual",
            "links": list(links.values())[:30]}, url


def mount_mobile_capture(router, sessions, admin, enqueue):
    mutex = asyncio.Lock()

    @router.post("/mobile-import", dependencies=[Depends(admin)])
    async def mobile_import(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 1_500_000:
                raise HTTPException(413, "提交内容过大，请只保留文章正文。")
            raw.extend(chunk)
        try:
            body = MobileCapture.model_validate_json(raw)
        except ValidationError:
            # Do not echo user-submitted page text or unknown credential fields.
            raise HTTPException(422, "正文格式无效，请提交标题、文章链接和 80 至 60000 字正文。") from None
        # Serialise duplicate taps; the content-addressed analysis/translation cache is shared.
        async with mutex:
            with sessions.begin() as session:
                doc = session.get(WebDocument, body.document_id)
                if not doc or not session.scalar(select(ArticleDocument).where(
                        ArticleDocument.document_id == doc.id).limit(1)):
                    raise HTTPException(404, "目标文章不存在")
                parsed, url = validate_capture(body, doc)
                digest = fingerprint(parsed["title"], parsed["text"], parsed["partial"])
                capture = session.get(DocumentCapture, doc.id)
                if (capture and capture.content_hash == digest and doc.content_hash == digest and capture.job_id
                        and parsed["links"] == doc.links):
                    return {"ready": True, "already_saved": True, "job_id": capture.job_id,
                            "message": "这份正文已经保存，无需重复提交或翻译。"}
                save_capture(session, doc, {"status": "fetched", "url": url, "document": parsed})
                doc.content_type = "text/plain"
                if not capture:
                    capture = DocumentCapture(document_id=doc.id)
                    session.add(capture)
                capture.method, capture.source_url = body.method, url
                capture.content_hash, capture.captured_at, capture.job_id = digest, now_iso(), ""
            # Does not mark WebsiteAccess ready: device cookies and network remain on the device.
            uid = enqueue()
            with sessions.begin() as session:
                session.get(DocumentCapture, body.document_id).job_id = uid
            return {"ready": True, "already_saved": False, "job_id": uid,
                    "message": "正文已保存，正在生成中文解读；网站登录状态仍留在采集设备。"}
