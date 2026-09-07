import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_radar import item

from radar.api import create_app
from radar.browser_access import browser_fallback
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
async def test_fallback_only_explicitly_enabled_domains_and_pauses_expired_auth(app):
    class Worker:
        calls = 0
        async def request(self, *args):
            self.calls += 1
            return {"status": "auth_required", "message": "需要重新登录"}
    client = Worker()
    assert await browser_fallback(app.state.sessions, "https://example.org/ai", client) is None
    assert client.calls == 0
    with app.state.sessions.begin() as s:
        s.add(WebsiteAccess(domain="example.org", enabled=True, status="ready"))
    with pytest.raises(PageUnavailable):
        await browser_fallback(app.state.sessions, "https://example.org/ai", client)
    assert await browser_fallback(app.state.sessions, "https://example.org/other", client) is None
    assert client.calls == 1


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
