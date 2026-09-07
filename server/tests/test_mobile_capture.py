import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_radar import item

from radar.api import create_app
from radar.config import Settings
from radar.models import Article, DocumentCapture, WebDocument, WebsiteAccess
from radar.pipeline import ingest
from radar.reading import fingerprint, sync_documents


@pytest.fixture
def mobile_app(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('anthropic_news_enabled=false\n[provider]\nkind="extractive"\n')
    app = create_app(Settings(config_path=str(config), database_url=f"sqlite:///{tmp_path}/db",
                             reader_token="reader", admin_token="admin"))
    with app.state.sessions.begin() as session:
        ingest(session, [item(platform="web", url="https://example.org/ai", external_id="web")],
               app.state.pipeline.config)
        sync_documents(session, session.scalar(select(Article)), app.state.pipeline.config)
        session.add(WebsiteAccess(domain="example.org", enabled=False, status="access_restricted"))
    app.state.queued = []
    async def run(**kwargs):
        app.state.queued.append(kwargs)
    monkeypatch.setattr(app.state.pipeline, "run", run)
    return app


def capture(**kwargs):
    return {"document_id": fingerprint("https://example.org/ai"), "url": "https://example.org/ai",
            "title": "Research on AI agents", "text": "Researchers report an experiment with AI agents. " * 8,
            "links": [{"url": "https://example.org/paper", "label": "Paper"}], "partial": False, **kwargs}


def test_mobile_capture_requires_admin_and_bound_target(mobile_app):
    with TestClient(mobile_app) as client:
        assert client.post("/v1/browser/mobile-import", json=capture()).status_code == 401
        assert client.post("/v1/browser/mobile-import", json=capture(),
                           headers={"Authorization": "Bearer reader"}).status_code == 403
        client.headers["Authorization"] = "Bearer admin"
        assert client.post("/v1/browser/mobile-import", json=capture(document_id="a" * 64)).status_code == 404
        assert client.post("/v1/browser/mobile-import", json=capture(url="https://evil.test/ai")).status_code == 422
        assert client.post("/v1/browser/mobile-import", json=capture(url="https://example.org/login")).status_code == 422
        assert client.post("/v1/browser/mobile-import", json=capture(url="file:///etc/passwd")).status_code == 422
        assert client.post("/v1/browser/mobile-import", json=capture(cookie="private")).status_code == 422
        assert client.post("/v1/browser/mobile-import", json=capture(text="x" * 60001)).status_code == 422
        assert client.post("/v1/browser/mobile-import", content=b"x" * 1_500_001).status_code == 413
    with mobile_app.state.sessions() as session:
        assert session.get(WebDocument, capture()["document_id"]).text == ""
    assert not mobile_app.state.queued


def test_mobile_capture_deduplicates_and_never_authorizes_server(mobile_app):
    with TestClient(mobile_app) as client:
        client.headers["Authorization"] = "Bearer admin"
        first = client.post("/v1/browser/mobile-import", json=capture()).json()
        duplicate = client.post("/v1/browser/mobile-import", json=capture()).json()
        assert first["ready"] and not first["already_saved"]
        assert duplicate["already_saved"] and duplicate["job_id"] == first["job_id"]
        with mobile_app.state.sessions() as session:
            doc = session.get(WebDocument, capture()["document_id"])
            assert doc.text == capture()["text"].strip() and doc.status == "fetched"
            metadata = session.get(DocumentCapture, doc.id)
            assert metadata.method == "mobile_browser" and metadata.content_hash == doc.content_hash
            assert metadata.job_id == first["job_id"]
            access = session.get(WebsiteAccess, "example.org")
            assert not access.enabled and access.status == "access_restricted"
            assert not access.verified_at
    assert len(mobile_app.state.queued) == 1 and mobile_app.state.queued[0]["kind"] == "read"


def test_reject_challenge_and_accept_manual_selection_with_known_url_alias(mobile_app):
    with TestClient(mobile_app) as client:
        client.headers["Authorization"] = "Bearer admin"
        for body in [capture(title="Just a moment..."), capture(text="Verify you are human. " * 10)]:
            assert client.post("/v1/browser/mobile-import", json=body).status_code == 422
        result = client.post("/v1/browser/mobile-import", json=capture(
            method="manual", url="http://www.example.org/ai/?utm_source=phone", links=[
                {"url": "javascript:alert(1)", "label": "Invalid"},
                {"url": "https://example.org/login", "label": "Log in"}]))
        assert result.status_code == 200
        with mobile_app.state.sessions() as session:
            doc = session.get(WebDocument, capture()["document_id"])
            assert doc.partial and doc.links == []
            assert session.get(DocumentCapture, doc.id).method == "manual"


def test_import_reuses_existing_content_addressed_translation(mobile_app):
    from radar.models import Translation
    from radar.translation import cache_key
    body = capture()
    body["text"] = body["text"].strip()
    key = cache_key(body["title"], body["text"], mobile_app.state.pipeline.config.translation)
    with mobile_app.state.sessions.begin() as session:
        translation = Translation(id=key, original_title=body["title"], original_text=body["text"],
                                  title_zh="智能体研究", text_zh="已校对的中文正文", status="ready", revision="test",
                                  attempts=2, updated_at="2026-09-07T00:00:00+00:00")
        session.add(translation)
    with TestClient(mobile_app) as client:
        assert client.post("/v1/browser/mobile-import", json=body,
                           headers={"Authorization": "Bearer admin"}).status_code == 200
    with mobile_app.state.sessions() as session:
        translation = session.get(Translation, key)
        assert translation.attempts == 2 and translation.text_zh == "已校对的中文正文"
        assert translation.updated_at == "2026-09-07T00:00:00+00:00"


def test_mobile_capture_refreshes_links_without_reusing_stale_analysis(mobile_app):
    with mobile_app.state.sessions.begin() as session:
        doc = session.get(WebDocument, capture()["document_id"])
        doc.content_hash, doc.analysis_id = "old-content", "old-summary"
    with TestClient(mobile_app) as client:
        client.headers["Authorization"] = "Bearer admin"
        first = client.post("/v1/browser/mobile-import", json=capture()).json()
        with mobile_app.state.sessions() as session:
            assert session.get(WebDocument, capture()["document_id"]).analysis_id == ""
        changed = capture(links=[{"url": "https://example.org/research", "label": "Research"}])
        second = client.post("/v1/browser/mobile-import", json=changed).json()
        assert not second["already_saved"] and second["job_id"] != first["job_id"]
        with mobile_app.state.sessions() as session:
            assert session.get(WebDocument, capture()["document_id"]).links == changed["links"]
    assert len(mobile_app.state.queued) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["fetched", "failed", "browser"])
async def test_inflight_server_read_cannot_overwrite_new_phone_capture(mobile_app, monkeypatch, outcome):
    import asyncio

    import httpx

    from radar.web_reader import PageFetcher, PageUnavailable
    started, release = asyncio.Event(), asyncio.Event()
    old = {"title": "Older server response", "text": "Older server article content. " * 10,
           "links": [], "partial": False}

    async def bytes_read(self, url, **kwargs):
        if outcome == "browser":
            raise ValueError("dynamic page")
        started.set()
        await release.wait()
        if outcome == "failed":
            raise PageUnavailable("access_restricted", "Older challenge")
        return url, 200, {"content-type": "text/html"}, b"old source"

    async def robots(self, url):
        pass

    async def extract(*args):
        return old

    async def fallback(*args):
        started.set()
        await release.wait()
        return {"status": "fetched", "url": capture()["url"], "document": old}

    monkeypatch.setattr(PageFetcher, "bytes", bytes_read)
    monkeypatch.setattr(PageFetcher, "check_robots", robots)
    monkeypatch.setattr("radar.reading.extract_page", extract)
    monkeypatch.setattr("radar.browser_access.browser_fallback", fallback)
    task = asyncio.create_task(mobile_app.state.pipeline.reading.fetch_one(capture()["document_id"], PageFetcher()))
    await asyncio.wait_for(started.wait(), 2)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mobile_app), base_url="https://radar.test") as client:
            response = await client.post("/v1/browser/mobile-import", json=capture(),
                                         headers={"Authorization": "Bearer admin"})
            assert response.status_code == 200
    finally:
        release.set()
        await task
    with mobile_app.state.sessions() as session:
        doc = session.get(WebDocument, capture()["document_id"])
        assert doc.text == capture()["text"].strip() and doc.status == "fetched"
        assert session.get(DocumentCapture, doc.id).content_hash == doc.content_hash
