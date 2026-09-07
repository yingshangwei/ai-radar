from datetime import UTC, datetime, timedelta

import pytest
from fastapi import APIRouter, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from radar.db import database
from radar.mobile_queue import mount_mobile_queue
from radar.models import Article, ArticleDocument, WebDocument, WebsiteAccess
from radar.reading import fingerprint


@pytest.fixture
def queue(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/queue.db")
    app, router = FastAPI(), APIRouter(prefix="/v1/browser")
    def admin(authorization: str = Header(default="")):
        if authorization == "Bearer reader":
            raise HTTPException(403)
        if authorization != "Bearer admin":
            raise HTTPException(401)
    mount_mobile_queue(router, sessions, admin)
    app.include_router(router)
    with TestClient(app) as client:
        yield client, sessions
    engine.dispose()


def add(sessions, url, *, minutes=0, status="pending", text="", bound=True, reference=""):
    key = fingerprint(url)
    with sessions.begin() as session:
        if not session.get(WebDocument, key):
            session.add(WebDocument(id=key, url=url, status=status, text=text))
        if bound:
            article_id = fingerprint(url, reference)
            session.add(Article(
                id=article_id, platform="web", source_id="test", external_id=article_id,
                url=url, canonical_url=url, title="AI source", text="An AI source document.",
                author="Source", published_at=(datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(),
            ))
            session.flush()
            session.add(ArticleDocument(article_id=article_id, document_id=key, relation="source", label=""))
    return key


def get(client, domains=None, **params):
    if domains is not None:
        params["domains"] = domains
    return client.get("/v1/browser/mobile-queue", params=params, headers={"Authorization": "Bearer admin"})


def test_mobile_queue_is_admin_only_even_with_no_domains(queue):
    client, _ = queue
    assert client.get("/v1/browser/mobile-queue").status_code == 401
    assert client.get("/v1/browser/mobile-queue", headers={"Authorization": "Bearer reader"}).status_code == 403
    assert get(client).json() == []
    assert get(client, "").json() == []


def test_only_exact_permitted_domains_and_www_aliases_are_queued(queue):
    client, sessions = queue
    www = add(sessions, "https://www.example.org/ai", minutes=3)
    exact = add(sessions, "https://example.org/ai", minutes=2)
    second = add(sessions, "https://publisher.net/article", minutes=1)
    add(sessions, "https://sub.example.org/ai")
    add(sessions, "https://example.org.evil.net/ai")
    add(sessions, "https://notexample.org/ai")
    response = get(client, "EXAMPLE.ORG,www.publisher.net", limit=5)
    assert response.status_code == 200
    assert [row["document_id"] for row in response.json()] == [second, exact, www]
    assert {row["domain"] for row in response.json()} == {"publisher.net", "example.org", "www.example.org"}
    assert all(set(row) == {"document_id", "url", "domain"} for row in response.json())


def test_latest_association_wins_and_bound_documents_are_deduplicated(queue):
    client, sessions = queue
    keys = [add(sessions, f"https://example.org/article-{i}", minutes=i + 10) for i in range(7)]
    add(sessions, "https://example.org/article-6", reference="new mention", minutes=0)
    result = get(client, "example.org").json()
    assert [row["document_id"] for row in result] == [keys[6], keys[0], keys[1]]
    result = get(client, "example.org", limit=5).json()
    assert len(result) == 5 and len({row["document_id"] for row in result}) == 5
    assert get(client, "example.org", limit=0).status_code == 422
    assert get(client, "example.org", limit=6).status_code == 422


def test_finished_prohibited_oversize_rate_limited_and_unbound_documents_are_excluded(queue):
    client, sessions = queue
    for status in ["blocked", "restricted", "too_large", "rate_limited"]:
        add(sessions, f"https://example.org/{status}", status=status)
    add(sessions, "https://example.org/fetched", text="Acquired source text", status="fetched")
    add(sessions, "https://example.org/stale-failure", text="Retained source text", status="unavailable")
    add(sessions, "https://example.org/orphan", bound=False)
    pending = add(sessions, "https://example.org/pending")
    assert [row["document_id"] for row in get(client, "example.org", limit=5).json()] == [pending]


def test_server_challenge_and_server_backoff_do_not_block_permitted_phone(queue):
    client, sessions = queue
    login = add(sessions, "https://example.org/login-needed", status="auth_required", minutes=2)
    challenged = add(sessions, "https://example.org/challenged", status="access_restricted", minutes=1)
    with sessions.begin() as session:
        session.add(WebsiteAccess(domain="example.org", status="access_restricted", enabled=False))
        session.get(WebDocument, challenged).retry_at = "2099-01-01T00:00:00+00:00"
    assert [row["document_id"] for row in get(client, "example.org").json()] == [challenged, login]


@pytest.mark.parametrize("domains", [
    "https://example.org", "example.org:443", "*.example.org", "example.org/path", "example.org?x=y",
    "example.org#x", "user@example.org", "127.0.0.1", "::1", "localhost", "site.local", "site.internal",
    "site.localhost", "example.org,", "bad_label.org", "-example.org", "example-.org", "example.org.",
    ",example.org", " ", ".example.org", "example..org", "a" * 64 + ".org", "127.1", "0177.0.0.1",
])
def test_invalid_domain_permissions_are_rejected(queue, domains):
    client, _ = queue
    assert get(client, domains).status_code == 400


def test_permission_count_and_total_length_are_bounded(queue):
    client, _ = queue
    assert get(client, ",".join(f"site{i}.org" for i in range(30))).json() == []
    assert get(client, ",".join(f"site{i}.org" for i in range(31))).status_code == 400
    assert get(client, "a" * 8001).status_code == 422
