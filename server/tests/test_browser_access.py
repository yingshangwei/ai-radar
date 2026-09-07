import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_radar import item

from radar.api import create_app
from radar.browser_access import browser_fallback, save_capture, sites
from radar.browser_worker import public_proxy
from radar.config import Settings
from radar.models import ArticleDocument, WebDocument, WebsiteAccess
from radar.pipeline import ingest
from radar.reading import fingerprint, sync_documents
from radar.web_reader import PageFetcher, PageUnavailable


@pytest.fixture
def app(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('anthropic_news_enabled=false\n[provider]\nkind="extractive"\n')
    app = create_app(Settings(config_path=str(config), database_url=f"sqlite:///{tmp_path}/db",
                             reader_token="reader", admin_token="admin", browser_worker_token="w" * 32,
                             browser_public_origin="https://radar.test"))
    with app.state.sessions.begin() as s:
        ingest(s, [item(platform="web", url="https://example.org/ai", external_id="web")], app.state.pipeline.config)
        from radar.models import Article
        sync_documents(s, s.scalar(select(Article)), app.state.pipeline.config)
    return app


def headers(token="admin", origin=True):
    return {"Authorization": "Bearer " + token, **({"Origin": "https://radar.test"} if origin else {})}


@pytest.mark.asyncio
async def test_401_and_403_are_distinct(monkeypatch, respx_mock):
    async def addresses(*_):
        return ["93.184.216.34"]
    monkeypatch.setattr("radar.web_reader.public_addresses", addresses)
    for code, status in [(401, "auth_required"), (403, "access_restricted"), (429, "rate_limited")]:
        respx_mock.get("https://93.184.216.34/article").respond(code)
        with pytest.raises(PageUnavailable) as error:
            await PageFetcher().bytes("https://example.org/article", check_robots=False)
        assert error.value.status == status


def test_session_access_ticket_replay_origin_and_expiry(app, monkeypatch):
    uid = "session-id"
    calls = []
    async def worker(method, path, data=None):
        calls.append(path)
        return {"id": uid, "expires": time.time() + 1200}
    monkeypatch.setattr(app.state.browser_client, "request", worker)
    with TestClient(app, base_url="https://radar.test") as c:
        body = {"document_id": fingerprint("https://example.org/ai")}
        assert c.post("/v1/browser/sessions", json=body).status_code == 401
        assert c.post("/v1/browser/sessions", json=body, headers=headers("reader")).status_code == 403
        assert not calls
        assert c.post("/v1/browser/sessions", json={"document_id": "a" * 64}, headers=headers()).status_code == 404
        result = c.post("/v1/browser/sessions", json=body, headers=headers()).json()
        ticket = result["path"].split("ticket=")[1]
        assert ticket not in str(app.state.browser_sessions)
        base = f"/v1/browser/view/{uid}/"
        assert c.post(base + "exchange", json={"ticket": ticket}, headers={"Origin": "https://evil.test"}).status_code == 403
        assert c.post(base + "finish", headers=headers()).status_code == 401
        r = c.post(base + "exchange", json={"ticket": ticket}, headers=headers())
        assert r.status_code == 200
        assert "HttpOnly" in r.headers["set-cookie"] and "Secure" in r.headers["set-cookie"]
        assert f"Path={base}" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]
        assert c.post(base + "exchange", json={"ticket": ticket}, headers=headers()).status_code == 401
        assert c.post(base + "finish", headers={"Origin": "https://evil.test"}).status_code == 403
        app.state.browser_sessions[uid]["expires"] = time.time() - 1
        assert c.post(base + "finish", headers=headers()).status_code == 410
        assert calls == ["/sessions"]


def test_finish_verifies_article_before_enabling_and_queues_work(app, monkeypatch):
    result = {"status": "access_restricted", "message": "仍需验证"}
    uid, queued = "session-test", []
    async def worker(method, path, data=None):
        if path == "/sessions":
            return {"id": uid, "expires": time.time() + 1200}
        return result
    async def run(**kwargs):
        queued.append(kwargs)
    monkeypatch.setattr(app.state.browser_client, "request", worker)
    monkeypatch.setattr(app.state.pipeline, "run", run)
    key = fingerprint("https://example.org/ai")
    with TestClient(app, base_url="https://radar.test") as c:
        response = c.post("/v1/browser/sessions", json={"document_id": key}, headers=headers()).json()
        base = f"/v1/browser/view/{uid}/"
        ticket = response["path"].split("ticket=")[1]
        c.post(base + "exchange", json={"ticket": ticket}, headers=headers())
        assert c.post(base + "finish", headers=headers()).json()["ready"] is False
        with app.state.sessions() as s:
            assert not s.get(WebsiteAccess, "example.org").enabled
            assert not s.get(WebDocument, key).text
        assert not queued
        result = {"status": "fetched", "url": "https://example.org/ai", "document": {
            "title": "AI source", "text": "A grounded AI source with evidence. " * 10, "links": [], "partial": False}}
        response = c.post(base + "finish", headers=headers())
        assert response.status_code == 200 and response.json()["ready"]
        assert response.json()["job_id"]
        with app.state.sessions() as s:
            assert s.get(WebsiteAccess, "example.org").enabled
            assert s.get(WebDocument, key).text == result["document"]["text"]
            assert s.scalar(select(ArticleDocument)).document_id == key
        assert uid not in app.state.browser_sessions
    assert len(queued) == 1 and queued[0]["kind"] == "read"


@pytest.mark.asyncio
async def test_public_fallback_is_automatic_and_challenge_pauses_domain(app):
    class Worker:
        calls = 0
        async def request(self, *args):
            self.calls += 1
            await asyncio.sleep(0)
            return {"status": "auth_required", "message": "需要重新登录"}
    client = Worker()
    attempts = await asyncio.gather(
        browser_fallback(app.state.sessions, "https://example.org/ai", client),
        browser_fallback(app.state.sessions, "https://example.org/other", client), return_exceptions=True)
    assert isinstance(attempts[0], PageUnavailable) and attempts[0].status == "auth_required"
    assert attempts[1] is None
    assert await browser_fallback(app.state.sessions, "https://example.org/other", client) is None
    assert client.calls == 1
    with app.state.sessions() as s:
        assert s.get(WebsiteAccess, "example.org").status == "auth_required"


@pytest.mark.asyncio
async def test_public_browser_success_does_not_require_manual_verification(app):
    class Worker:
        async def request(self, method, path, data):
            assert (method, path, data) == ("POST", "/fetch", {"url": "https://example.org/ai"})
            return {"status": "fetched", "url": data["url"], "document": {"text": "source " * 30}}
    result = await browser_fallback(app.state.sessions, "https://example.org/ai", Worker())
    assert result["status"] == "fetched"
    with app.state.sessions() as s:
        access = s.get(WebsiteAccess, "example.org")
        assert access.enabled and access.status == "ready"
        assert not access.verified_at  # Public reading is not a claim of user authorization.


@pytest.mark.asyncio
@pytest.mark.parametrize("status,enabled", [("ready", False), ("paused", False), ("access_restricted", True)])
async def test_user_pause_and_known_access_restriction_do_not_open_browser(app, status, enabled):
    class Worker:
        async def request(self, *args):
            pytest.fail("A paused or challenged domain must not be opened automatically")
    with app.state.sessions.begin() as s:
        s.add(WebsiteAccess(domain="example.org", status=status, enabled=enabled))
    assert await browser_fallback(app.state.sessions, "https://example.org/ai", Worker()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["restricted", "blocked", "too_large", "rate_limited"])
async def test_one_document_limit_does_not_disable_other_public_pages(app, status):
    class Worker:
        calls = 0
        async def request(self, *_args):
            self.calls += 1
            return {"status": status if self.calls == 1 else "fetched", "message": "Document restriction"}
    client = Worker()
    with pytest.raises(PageUnavailable) as error:
        await browser_fallback(app.state.sessions, "https://example.org/ai", client)
    assert error.value.status == status
    assert (await browser_fallback(app.state.sessions, "https://example.org/other", client))["status"] == "fetched"
    assert client.calls == 2


def test_site_with_complete_text_needs_no_authorization(app):
    with app.state.sessions.begin() as s:
        doc = s.scalar(select(WebDocument))
        doc.text, doc.status = "Public AI article. " * 20, "fetched"
        # An old authorization warning cannot negate a successful public HTTP read.
        s.add(WebsiteAccess(domain="example.org", status="access_restricted", enabled=False))
    with app.state.sessions() as s:
        site = sites(s)[0]
    assert site["status"] == "fetched" and site["action"] == "none"
    assert site["fetched"] == 1 and site["pending"] == 0
    assert site["access_status"] == "access_restricted" and not site["enabled"]
    assert "无需授权" in site["message"]


@pytest.mark.parametrize("changed", [True, False])
def test_new_browser_text_never_uses_old_ready_analysis(app, changed):
    text = "A grounded AI article with primary evidence. " * 8
    result = {"status": "fetched", "url": "https://example.org/ai", "document": {
        "title": "Source", "text": text, "links": [], "partial": False}}
    with app.state.sessions.begin() as s:
        doc = s.scalar(select(WebDocument))
        doc.title, doc.text, doc.partial = "Source", "Previous version" if changed else text, False
        doc.content_hash = fingerprint(doc.title, doc.text, doc.partial)
        doc.analysis_id = "previous-ready-summary"
        save_capture(s, doc, result)
        assert doc.analysis_id == ("" if changed else "previous-ready-summary")


@pytest.mark.parametrize("status,action", [
    ("pending", "automatic"), ("unavailable", "retry"), ("unsupported", "retry"),
    ("rate_limited", "retry"), ("auth_required", "login"), ("access_restricted", "restricted"),
    ("restricted", "restricted"), ("blocked", "restricted"), ("too_large", "restricted"),
])
def test_sites_distinguish_auto_reading_and_real_intervention(app, status, action):
    with app.state.sessions.begin() as s:
        doc = s.scalar(select(WebDocument))
        doc.status, doc.retry_at = status, "2026-09-08T00:00:00+00:00"
    with app.state.sessions() as s:
        site = sites(s)[0]
    assert site["status"] == status and site["action"] == action
    assert site["counts"][action] == 1 and site["fetched"] == 0
    assert site["automatic"] == (action in {"automatic", "retry"})
    assert site["retry_at"] == "2026-09-08T00:00:00+00:00"


def test_site_user_browser_pause_is_explicit(app):
    with app.state.sessions.begin() as s:
        s.add(WebsiteAccess(domain="example.org", enabled=False, status="ready"))
    with app.state.sessions() as s:
        site = sites(s)[0]
    assert site["status"] == "paused" and site["action"] == "none"
    assert not site["browser_enabled"]


@pytest.mark.asyncio
async def test_browser_proxy_pins_upstream_and_rejects_private_targets(monkeypatch):
    connections = []
    async def addresses(host, port):
        if host == "private.test":
            raise PageUnavailable("blocked", "private")
        return ["93.184.216.34"]
    async def connect(host, port):
        connections.append((host, port))
        raise OSError("test connection ends here")
    class Writer:
        closed = False
        def close(self):
            self.closed = True
    monkeypatch.setattr("radar.browser_worker.public_addresses", addresses)
    monkeypatch.setattr("radar.browser_worker.asyncio.open_connection", connect)
    for target in ["private.test:443", "public.test:22", "public.test:443"]:
        reader = asyncio.StreamReader()
        reader.feed_data(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
        reader.feed_eof()
        writer = Writer()
        await public_proxy(reader, writer)
        assert writer.closed
    assert connections == [("93.184.216.34", 443)]


def test_only_bound_documents_can_launch_browser(app, monkeypatch):
    with app.state.sessions.begin() as s:
        s.add(WebDocument(id="a" * 64, url="https://example.org/orphan"))
    with TestClient(app, base_url="https://radar.test") as c:
        assert c.post("/v1/browser/sessions", json={"document_id": "a" * 64}, headers=headers()).status_code == 404
        assert c.get("/v1/browser/novnc/..%2F..%2Fetc%2Fpasswd").status_code == 404
        assert c.get("/v1/browser/permissions", headers=headers("reader")).status_code == 403
        assert c.get("/v1/browser/permissions", headers=headers()).json() == {"manage": True}


@pytest.mark.asyncio
async def test_completed_challenge_not_reloaded_and_login_page_not_captured(monkeypatch):
    from radar.browser_worker import Browser
    class Page:
        url = "https://example.org/article"
        navigation_count = 0
        async def goto(self, url, **kwargs):
            self.navigation_count += 1
            # A login page which keeps redirecting must never become article evidence.
            self.url = "https://example.org/login"
        async def wait_for_timeout(self, ms):
            pass
        def locator(self, selector):
            return self
        async def count(self):
            return 0
        async def title(self):
            return "Readable AI article"
        async def content(self):
            return "<article>real source</article>"
    async def extract(*_):
        return {"title": "AI article", "text": "source " * 30, "links": [], "partial": False}
    class Rules:
        async def check_robots(self, url):
            pass
    monkeypatch.setattr("radar.browser_worker.extract_page", extract)
    browser = Browser()
    browser.page, browser.last_status, browser.robots = Page(), 200, Rules()
    assert (await browser.capture("https://example.org/article"))["status"] == "fetched"
    assert browser.page.navigation_count == 0
    browser.page.url = "https://example.org/dashboard"
    assert (await browser.capture("https://example.org/article"))["status"] == "auth_required"
    assert browser.page.navigation_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("final,redirect", [
    ("https://www.example.org/article/", False),
    ("https://example.org/news/canonical-article", True),
    ("https://publisher.example.net/news/canonical-article", True),
])
async def test_public_www_and_observed_http_canonical_redirects_are_readable(monkeypatch, final, redirect):
    from radar.browser_worker import Browser
    class Page:
        url = final
        async def goto(self, *_args, **_kwargs):
            pytest.fail("A verified canonical article must not be unnecessarily reloaded")
        async def wait_for_timeout(self, _ms):
            pass
        def locator(self, _selector):
            return self
        async def count(self):
            return 0
        async def title(self):
            return "Sign in with ChatGPT: Developer guide"
        async def content(self):
            return "<article>Public AI source.</article>"
    checked = []
    class Rules:
        async def check_robots(self, url):
            checked.append(url)
    async def extract(*_):
        return {"title": "Article", "text": "Public AI source. " * 20, "links": [], "partial": False}
    monkeypatch.setattr("radar.browser_worker.extract_page", extract)
    browser = Browser()
    browser.page, browser.last_status, browser.robots = Page(), 200, Rules()
    if redirect:
        browser.navigation_target, browser.navigation_final = "https://example.org/article", final
    result = await browser.capture("https://example.org/article")
    assert result["status"] == "fetched" and result["url"] == final
    assert checked == [final]


@pytest.mark.asyncio
@pytest.mark.parametrize("title,status", [("Just a moment...", "access_restricted"), ("Sign in", "auth_required")])
async def test_challenges_and_login_are_never_article_evidence(monkeypatch, title, status):
    from radar.browser_worker import Browser
    class Page:
        url = "https://example.org/article"
        async def wait_for_timeout(self, _ms):
            pass
        def locator(self, _selector):
            return self
        async def count(self):
            return 0
        async def title(self):
            return title
        async def content(self):
            pytest.fail("A challenge or login page must not be parsed as article text")
    browser = Browser()
    browser.page, browser.last_status = Page(), 200
    result = await browser.capture("https://example.org/article")
    assert result["status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect,robots_status,expected", [
    (False, "", "unavailable"), (True, "restricted", "restricted"), (True, "blocked", "blocked"),
])
async def test_unrelated_navigation_and_redirect_rules_cannot_be_saved(monkeypatch, redirect, robots_status, expected):
    from radar.browser_worker import Browser
    class Page:
        url = "https://example.org/other"
        async def goto(self, *_args, **_kwargs):
            pass
        async def wait_for_timeout(self, _ms):
            pass
        def locator(self, _selector):
            return self
        async def count(self):
            return 0
        async def title(self):
            return "An unrelated public page"
        async def content(self):
            pytest.fail("Unrelated or prohibited content must not be saved")
    class Rules:
        async def check_robots(self, _url):
            raise PageUnavailable(robots_status, "Public-page rule prohibits this document")
    browser = Browser()
    browser.page, browser.last_status, browser.robots = Page(), 200, Rules()
    if redirect:
        browser.navigation_target, browser.navigation_final = "https://example.org/article", browser.page.url
    result = await browser.capture("https://example.org/article")
    assert result["status"] == expected
