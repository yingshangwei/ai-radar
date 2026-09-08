"""Isolated browser service. No model keys, article database, or public listening ports.

Only the API can request URLs. Interactive sessions and background reads share a
per-domain profile, but never run concurrently. Browser traffic uses a DNS-pinned
public-only proxy, including redirects and subresources.
"""
import asyncio
import contextlib
import hashlib
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from pydantic import BaseModel, Field

from .browser_readiness import wait_for_content
from .links import normalize_link
from .page_parser import extract_page
from .web_reader import PageFetcher, PageUnavailable, public_addresses


def same_article(left, right):
    """Permit normal scheme/www/slash canonicalization without broadening the path."""
    a, b = urlsplit(normalize_link(left) or ""), urlsplit(normalize_link(right) or "")
    return bool(a.hostname and b.hostname and (
        a.hostname.removeprefix("www."), a.path.rstrip("/"), a.query
    ) == (b.hostname.removeprefix("www."), b.path.rstrip("/"), b.query))


def login_target(url):
    return bool(re.search(r"/(?:log-?in|sign-?in|auth|oauth|sso|dashboard|accounts?)(?:/|$)",
                          urlsplit(url).path, re.I))


async def relay(reader, writer):
    while chunk := await reader.read(65536):
        writer.write(chunk)
        await writer.drain()


async def public_proxy(reader, writer):
    upstream = None
    try:
        async with asyncio.timeout(90):
            raw = await reader.readuntil(b"\r\n\r\n")
            if len(raw) > 16384:
                return
            method, target, _ = raw.split(b"\r\n", 1)[0].decode("ascii").split(" ", 2)
            u = urlsplit("//" + target if method == "CONNECT" else target)
            port = u.port or (443 if method == "CONNECT" else 80)
            if port not in {80, 443} or not u.hostname or u.username or u.password:
                return
            if method != "CONNECT" and (method not in {"GET", "HEAD", "POST"} or u.scheme != "http"):
                return
            addresses = await public_addresses(u.hostname, port)
            remote, upstream = await asyncio.wait_for(asyncio.open_connection(addresses[0], port), 10)
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                # One origin per connection. Never forward proxy credentials or permit
                # an absolute-form second request to choose a different upstream.
                path = (u.path or "/") + (("?" + u.query) if u.query else "")
                headers = [line for line in raw.split(b"\r\n")[1:] if line and not line.lower().startswith(
                    (b"proxy-", b"connection:", b"host:"))]
                upstream.write(f"{method} {path} HTTP/1.1\r\nHost: {u.netloc}\r\nConnection: close\r\n".encode()
                               + b"\r\n".join(headers) + b"\r\n\r\n")
                await upstream.drain()
            tasks = [asyncio.create_task(relay(reader, upstream)), asyncio.create_task(relay(remote, writer))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, ValueError, UnicodeError, TimeoutError, PageUnavailable, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError):
        pass
    finally:
        if upstream:
            upstream.close()
        writer.close()


class Target(BaseModel):
    url: str = Field(max_length=4000)


class Browser:
    def __init__(self):
        self.root = Path(os.environ.get("RADAR_BROWSER_HOME", "/var/lib/ai-radar-browser"))
        self.lock = asyncio.Lock()
        self.context = None
        self.active = None
        self.processes = []
        self.playwright = None
        self.proxy = None
        self.navigation_target = self.navigation_final = ""
        self.navigation_failure = None
        self.extractor = os.environ.get("RADAR_BROWSER_EXTRACTOR", "trafilatura")
        if self.extractor not in {"crawl4ai", "trafilatura"}:
            raise RuntimeError("Unsupported browser extraction engine")
        self.background_domain = ""
        self.background_until = 0.0
        self.background_uses = 0
        self.engine_version = ""

    async def start(self):
        from playwright.async_api import async_playwright

        if self.extractor == "crawl4ai":
            from importlib.metadata import version

            self.engine_version = version("crawl4ai")
            if self.engine_version != "0.9.3":
                raise RuntimeError("Browser extraction dependency differs from the reviewed version")

        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        self.proxy = await asyncio.start_server(public_proxy, "127.0.0.1", 0, limit=16384)
        self.proxy_port = self.proxy.sockets[0].getsockname()[1]

    async def close(self):
        self.active = None
        self.background_domain, self.background_until, self.background_uses = "", 0.0, 0
        if self.context:
            with contextlib.suppress(Exception):
                await self.context.close()
            self.context = None
        for process in reversed(self.processes):
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        self.processes = []

    async def launch(self, url, interactive):
        url = normalize_link(url)
        if not url:
            raise HTTPException(400, "链接格式不受支持")
        self.navigation_target = self.navigation_final = ""
        self.navigation_failure = None
        self.robots = PageFetcher()
        await public_addresses(urlsplit(url).hostname, 443 if url.startswith("https:") else 80)
        domain = urlsplit(url).hostname
        if self.can_reuse(domain, interactive):
            self.background_uses += 1
            self.background_until = time.monotonic() + 90
            await self.navigate(url, interactive=False)
            return url
        await self.close()
        profile = self.root / "profiles" / hashlib.sha256(urlsplit(url).hostname.encode()).hexdigest()
        profile.mkdir(parents=True, mode=0o700, exist_ok=True)
        commands = [["Xvfb", ":89", "-screen", "0", "500x860x24", "-nolisten", "tcp", "-ac"],
                    ["openbox", "--sm-disable", "--config-file",
                     str(Path(__file__).with_name("browser_ui") / "openbox.xml")]]
        if interactive:
            commands.append(["x11vnc", "-display", ":89", "-localhost", "-rfbport", "15989", "-nopw", "-forever",
                             "-shared", "-noxdamage", "-quiet"])
        for command in commands:
            self.processes.append(await asyncio.create_subprocess_exec(
                *command, env={"DISPLAY": ":89", "HOME": str(self.root),
                                "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
            await asyncio.sleep(0.3)
            if self.processes[-1].returncode is not None:
                raise HTTPException(503, "浏览器显示服务未启动，请稍后重试")
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(profile), headless=False, chromium_sandbox=True,
            viewport={"width": 500, "height": 760}, accept_downloads=False,
            proxy={"server": f"http://127.0.0.1:{self.proxy_port}", "bypass": "<-loopback>"},
            env={"HOME": str(self.root), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "DISPLAY": ":89", "LANG": "C.UTF-8"},
            args=["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                  "--disable-extensions", "--disable-background-networking", "--disable-sync",
                  "--no-first-run", "--disable-save-password-bubble", "--window-size=500,860"],
        )
        # Reject file/data navigations and requests as well as normal private URLs.
        async def guard(route):
            u = urlsplit(route.request.url)
            if u.scheme not in {"http", "https"}:
                await route.abort()
                return
            try:
                await public_addresses(u.hostname, u.port or (443 if u.scheme == "https" else 80))
                # Background rendering must obey the same page rules as HTTP
                # reading, including new origins reached by a redirect.
                if not interactive and route.request.is_navigation_request() and (
                        route.request.frame == self.page.main_frame):
                    await self.robots.check_robots(route.request.url)
            except PageUnavailable as exc:
                if route.request.is_navigation_request() and route.request.frame == self.page.main_frame:
                    self.navigation_failure = exc
                await route.abort()
                return
            await route.continue_()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self.context.route("**/*", guard)
        self.last_status = 0
        def observe(response):
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self.last_status = response.status
                first = response.request
                while first.redirected_from:
                    first = first.redirected_from
                self.navigation_target = first.url
                self.navigation_final = response.url
        self.page.on("response", observe)
        for page in self.context.pages[1:]:
            await page.close()
        if not interactive:
            self.background_domain, self.background_uses = domain, 1
            self.background_until = time.monotonic() + 90
        await self.navigate(url, interactive)
        return url

    def can_reuse(self, domain, interactive=False):
        return bool(not interactive and not self.active and self.context and self.background_domain == domain
            and self.background_until > time.monotonic() and self.background_uses < 8
            and not self.page.is_closed())

    async def navigate(self, url, interactive):
        self.last_status = 0
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception:
            if not interactive:
                if self.navigation_failure:
                    raise self.navigation_failure from None
                raise HTTPException(502, "浏览器暂时无法打开目标文章") from None

    async def capture(self, url):
        # Stay on a verified article: reloading can trigger another site challenge.
        # Return from login/dashboard screens only when the target URL differs.
        try:
            def target_page(current):
                # A server-provided HTTP redirect chain from this exact target
                # is evidence of a canonical URL, unlike an arbitrary user
                # navigation to another same-domain page.
                return same_article(url, current) or (
                    same_article(url, self.navigation_target) and same_article(current, self.navigation_final))

            if not target_page(self.page.url):
                await self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
            readiness = await wait_for_content(self.page)
            if self.navigation_failure:
                raise self.navigation_failure
            final = normalize_link(self.page.url)
            if not final:
                return {"status": "blocked", "message": "目标不是可读取的公开网页。"}
            if self.last_status in {401, 403, 429}:
                status = {401: "auth_required", 403: "access_restricted", 429: "rate_limited"}[self.last_status]
                return {"status": status, "message": {
                    "auth_required": "目标文章要求登录或访问验证，已停止自动浏览器重试。",
                    "access_restricted": "网站暂时限制服务器访问，后台将在冷却后复查，也可在手机读取。",
                    "rate_limited": "网站限制访问频率，将在冷却后重试。",
                }[status]}
            if login_target(final) or await self.page.locator('input[type="password"]:visible').count():
                return {"status": "auth_required", "message": "页面仍在要求登录，未保存登录页面内容。"}
            title = (await self.page.title()).lower()
            if title.strip() in {"sign in", "log in", "login", "登录", "登入"} or title.startswith(("sign in -", "log in -")):
                return {"status": "auth_required", "message": "页面仍在要求登录，未保存登录页面内容。"}
            if any(marker in title for marker in ["just a moment", "access denied", "verify you", "security verification"]):
                return {"status": "access_restricted", "message": "网站正在进行人机验证，后台将在冷却后复查，也可在手机处理。"}
            if await self.page.locator('iframe[src*="challenges.cloudflare.com"]:visible, '
                                       'iframe[title*="challenge"]:visible').count():
                return {"status": "access_restricted", "message": "网站正在进行人机验证，后台将在冷却后复查，也可在手机处理。"}
            if not target_page(final):
                return {"status": "unavailable", "message": "网页跳转后尚无法确认目标文章，未将其他页面保存为正文。"}
            # An interactive session may navigate through a login provider, but
            # saving the eventual target article still honors its robots rules.
            await self.robots.check_robots(final)
            body = (await self.page.content()).encode()
            if len(body) > 8_000_000:
                return {"status": "too_large", "message": "页面超过单次读取大小限制。"}
            if self.extractor == "crawl4ai":
                from .crawl_reader import extract_crawl_page

                parsed = await extract_crawl_page(body, final)
            else:
                parsed = await extract_page(body, "text/html", final)
            if readiness and readiness.get("timed_out"):
                parsed["partial"] = True
            return {"status": "fetched", "url": final, "document": parsed,
                "extraction_engine": parsed.get("extraction_engine", "trafilatura"),
                "readiness_timed_out": bool(readiness and readiness.get("timed_out"))}
        except PageUnavailable as exc:
            return {"status": exc.status, "message": exc.message}
        except Exception:
            return {"status": "unavailable", "message": "暂未取得目标文章正文，稍后将自动重试。"}


def create_worker():
    browser = Browser()
    token = os.environ.get("RADAR_BROWSER_WORKER_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("A private browser worker token is required")

    async def auth(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401)

    @asynccontextmanager
    async def lifespan(app):
        await browser.start()
        async def expire():
            while True:
                await asyncio.sleep(10)
                async with browser.lock:
                    if browser.active and browser.active["expires"] <= time.time():
                        await browser.close()
                    elif not browser.active and browser.context and browser.background_until <= time.monotonic():
                        await browser.close()
        task = asyncio.create_task(expire())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await browser.close()
        browser.proxy.close()
        await browser.playwright.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health", dependencies=[Depends(auth)])
    async def health():
        return {"status": "ok", "interactive": bool(browser.active),
                "extraction_engine": browser.extractor, "engine_version": browser.engine_version,
                "background_browser_reusable": browser.can_reuse(browser.background_domain)}

    @app.post("/sessions", dependencies=[Depends(auth)])
    async def create(body: Target):
        async with browser.lock:
            if browser.active:
                raise HTTPException(409, "另一个网站正在授权，请先完成或关闭它")
            try:
                url = await browser.launch(body.url, True)
                browser.active = {"id": secrets.token_urlsafe(24), "url": url, "expires": time.time() + 1200}
                return browser.active
            except Exception:
                await browser.close()
                raise HTTPException(503, "浏览器暂时无法启动，请稍后重试") from None

    def active(uid):
        if not browser.active or browser.active["id"] != uid or browser.active["expires"] <= time.time():
            raise HTTPException(410, "授权窗口已关闭或过期，请重新打开")
        return browser.active

    @app.post("/sessions/{uid}/capture", dependencies=[Depends(auth)])
    async def capture(uid: str):
        async with browser.lock:
            current = active(uid)
            try:
                async with asyncio.timeout(70):
                    result = await browser.capture(current["url"])
            except TimeoutError:
                await browser.close()
                return {"status": "unavailable", "message": "网页读取超时，窗口已关闭，后台将继续重试。"}
            except BaseException:
                await browser.close()
                raise
            if result["status"] == "fetched":
                await browser.close()
            return result

    @app.post("/sessions/{uid}/close", dependencies=[Depends(auth)])
    async def close(uid: str):
        async with browser.lock:
            active(uid)
            await browser.close()
            return {"closed": True}

    @app.post("/fetch", dependencies=[Depends(auth)])
    async def fetch(body: Target):
        async with browser.lock:
            if browser.active:
                raise HTTPException(409, "浏览器正在由用户操作，稍后自动补采")
            try:
                async with asyncio.timeout(70):
                    url = await browser.launch(body.url, False)
                    result = await browser.capture(url)
                    if result["status"] != "fetched":
                        await browser.close()
                    return result
            except PageUnavailable as exc:
                await browser.close()
                return {"status": exc.status, "message": exc.message}
            except Exception:
                await browser.close()
                return {"status": "unavailable", "message": "浏览器读取暂时失败，下轮继续重试。"}
            except BaseException:
                await browser.close()
                raise

    @app.websocket("/sessions/{uid}/socket")
    async def socket(ws: WebSocket, uid: str):
        if not secrets.compare_digest(ws.headers.get("authorization", ""), "Bearer " + token):
            await ws.close(code=4401)
            return
        try:
            current = active(uid)
            reader, writer = await asyncio.open_connection("127.0.0.1", 15989)
        except Exception:
            await ws.close(code=4410)
            return
        await ws.accept()
        async def send():
            while chunk := await reader.read(65536):
                await ws.send_bytes(chunk)
        async def receive():
            while True:
                writer.write(await ws.receive_bytes())
                await writer.drain()
        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            await asyncio.wait(tasks, timeout=max(0, current["expires"] - time.time()),
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            with contextlib.suppress(Exception):
                await ws.close()
    return app
