"""Admin-only session creation, single-use tickets, scoped HttpOnly cookies and VNC proxy."""
import asyncio
import contextlib
import hashlib
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from .browser_access import BrowserClient, save_capture, sites
from .models import ArticleDocument, WebDocument, WebsiteAccess, now_iso
from .web_reader import PageUnavailable

STATIC = Path(__file__).with_name("browser_ui")


class CreateSession(BaseModel):
    document_id: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")


class Ticket(BaseModel):
    ticket: str = Field(min_length=32, max_length=128)


class SiteMode(BaseModel):
    enabled: bool


def mount_browser(app, settings, sessions, authenticated, admin, enqueue):
    router = APIRouter(prefix="/v1/browser")
    client = BrowserClient(settings)
    active = {}
    mutex = asyncio.Lock()
    app.state.browser_sessions = active
    app.state.browser_client = client
    origin = settings.browser_public_origin.rstrip("/")

    def checked_origin(request):
        # Explicit deployment origin prevents Host-header based origin bypasses.
        if not origin or request.headers.get("origin") != origin:
            raise HTTPException(403, "授权窗口来源不匹配")

    def current(uid):
        entry = active.get(uid)
        if not entry or entry["expires"] <= time.time():
            active.pop(uid, None)
            raise HTTPException(410, "授权窗口已过期，请重新打开")
        return entry

    def cookie_name(uid):
        return "radar_view_" + uid

    def cookie_auth(request, uid):
        entry = current(uid)
        value = request.cookies.get(cookie_name(uid), "")
        if not value or not secrets.compare_digest(hashlib.sha256(value.encode()).hexdigest(), entry["cookie"]):
            raise HTTPException(401, "请从 App 重新打开授权窗口")
        return entry

    async def worker(method, path, data=None):
        try:
            return await client.request(method, path, data)
        except PageUnavailable as exc:
            raise HTTPException({"busy": 409, "expired": 410}.get(exc.status, 503), exc.message) from None

    @router.get("/permissions", dependencies=[Depends(admin)])
    async def permissions():
        return {"manage": True}

    @router.get("/sites", dependencies=[Depends(authenticated)])
    async def list_sites():
        with sessions() as session:
            rows = sites(session)
        return {"enabled": bool(settings.browser_worker_token and origin), "items": rows,
                "active": [{"id": uid, "domain": e["domain"], "expires_at": e["expires"]}
                           for uid, e in active.items() if e["expires"] > time.time()]}

    @router.post("/sites/{domain}", dependencies=[Depends(admin)])
    async def mode(domain: str, body: SiteMode):
        with sessions.begin() as session:
            row = session.get(WebsiteAccess, domain)
            if not row:
                raise HTTPException(404, "请先在浏览器中验证此网站")
            if body.enabled and row.status != "ready":
                raise HTTPException(409, "请先验证目标文章可以读取")
            row.enabled, row.updated_at = body.enabled, now_iso()
        return {"enabled": body.enabled}

    @router.post("/sessions", dependencies=[Depends(admin)], status_code=201)
    async def create(body: CreateSession):
        if not origin:
            raise HTTPException(503, "网页授权服务尚未启用")
        with sessions() as session:
            doc = session.get(WebDocument, body.document_id)
            if not doc or not session.scalar(select(ArticleDocument).where(
                    ArticleDocument.document_id == doc.id).limit(1)):
                raise HTTPException(404, "目标文章不存在")
            if doc.status in {"blocked", "restricted"}:
                raise HTTPException(409, "该网页不允许自动读取，无法为其开启补采")
            url = doc.url
        async with mutex:
            for uid in list(active):
                if active[uid]["expires"] <= time.time():
                    active.pop(uid)
            result = await worker("POST", "/sessions", {"url": url})
            uid, ticket = result["id"], secrets.token_urlsafe(32)
            active[uid] = {"domain": urlsplit(url).hostname, "document_id": body.document_id,
                           "expires": result["expires"], "ticket": hashlib.sha256(ticket.encode()).hexdigest(),
                           "ticket_until": time.time() + 90, "cookie": "", "viewing": False}
        return {"id": uid, "domain": urlsplit(url).hostname, "expires_at": result["expires"],
                "path": f"/v1/browser/view/{uid}/#ticket={ticket}"}

    @router.post("/sessions/{uid}/close", dependencies=[Depends(admin)])
    async def close_admin(uid: str):
        current(uid)
        try:
            return await worker("POST", f"/sessions/{uid}/close")
        finally:
            active.pop(uid, None)

    @router.get("/view/{uid}/")
    async def viewer(uid: str):
        current(uid)
        return FileResponse(STATIC / "index.html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'",
        })

    @router.get("/assets/{name}")
    async def assets(name: str):
        if name not in {"viewer.js", "viewer.css"}:
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @router.get("/novnc/{path:path}")
    async def novnc(path: str):
        root = Path(settings.browser_assets_path).resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or target.suffix != ".js" or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target, media_type="text/javascript")

    @router.post("/view/{uid}/exchange")
    async def exchange(uid: str, body: Ticket, request: Request, response: Response):
        checked_origin(request)
        entry = current(uid)
        # No await between comparison and consumption: tickets can be used only once.
        digest = hashlib.sha256(body.ticket.encode()).hexdigest()
        if entry["ticket_until"] < time.time() or not secrets.compare_digest(entry["ticket"], digest):
            raise HTTPException(401, "授权入口已使用或过期，请从 App 重新打开")
        value = secrets.token_urlsafe(32)
        entry["ticket"], entry["cookie"] = "", hashlib.sha256(value.encode()).hexdigest()
        response.set_cookie(cookie_name(uid), value, max_age=max(1, int(entry["expires"] - time.time())),
                            httponly=True, secure=origin.startswith("https:"), samesite="strict",
                            path=f"/v1/browser/view/{uid}/")
        response.headers["Cache-Control"] = "no-store"
        return {"domain": entry["domain"], "expires_at": entry["expires"]}

    @router.post("/view/{uid}/finish")
    async def finish(uid: str, request: Request):
        checked_origin(request)
        entry = cookie_auth(request, uid)
        result = await worker("POST", f"/sessions/{uid}/capture")
        with sessions.begin() as session:
            access = session.get(WebsiteAccess, entry["domain"])
            if not access:
                access = WebsiteAccess(domain=entry["domain"])
                session.add(access)
            access.updated_at = now_iso()
            if result["status"] != "fetched":
                access.status = result["status"]
                access.message = result.get("message", "目标文章暂时无法读取")
                return {"ready": False, "message": access.message}
            doc = session.get(WebDocument, entry["document_id"])
            save_capture(session, doc, result)
            access.enabled, access.status, access.verified_at = True, "ready", now_iso()
            access.message = "已验证目标文章可读取，后续自动复用浏览器登录状态。"
            # Retry only this domain's missing documents; preserve all saved translations.
            for other in session.scalars(select(WebDocument).where(WebDocument.text == "")):
                if urlsplit(other.url).hostname == entry["domain"]:
                    other.retry_at = ""
        active.pop(uid, None)
        job_id = enqueue()
        return {"ready": True, "job_id": job_id, "message": "正文已保存，正在自动补采并生成中文解读。"}

    @router.post("/view/{uid}/close")
    async def close_view(uid: str, request: Request):
        checked_origin(request)
        cookie_auth(request, uid)
        try:
            return await worker("POST", f"/sessions/{uid}/close")
        finally:
            active.pop(uid, None)

    @router.websocket("/view/{uid}/socket")
    async def socket(ws: WebSocket, uid: str):
        try:
            checked_origin(ws)
            entry = cookie_auth(ws, uid)
            if entry["viewing"]:
                raise HTTPException(409)
        except HTTPException:
            await ws.close(code=4401)
            return
        import websockets
        entry["viewing"] = True
        url = settings.browser_worker_url.replace("http://", "ws://").replace("https://", "wss://")
        try:
            async with websockets.connect(url + f"/sessions/{uid}/socket", max_size=2_000_000,
                    additional_headers={"Authorization": "Bearer " + settings.browser_worker_token}) as remote:
                await ws.accept()
                async def to_client():
                    async for chunk in remote:
                        await ws.send_bytes(chunk)
                async def to_worker():
                    while True:
                        await remote.send(await ws.receive_bytes())
                tasks = [asyncio.create_task(to_client()), asyncio.create_task(to_worker())]
                try:
                    await asyncio.wait(tasks, timeout=max(0, entry["expires"] - time.time()),
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            pass
        finally:
            entry["viewing"] = False
            with contextlib.suppress(Exception):
                await ws.close()

    app.include_router(router)
