from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from radar.market_api import mount_market


@pytest.fixture
def market(monkeypatch):
    calls = []
    response = {"status": 200, "body": {"schema": 1, "collectorHost": "server"}}

    def handle(request):
        calls.append(request)
        return httpx.Response(response["status"], json=response["body"])

    original = httpx.AsyncClient
    monkeypatch.setattr("radar.market_api.httpx.AsyncClient", lambda **kw: original(
        **kw, transport=httpx.MockTransport(handle)))

    def reader(authorization: str | None = Header(default=None)):
        if authorization != "Bearer reader":
            raise HTTPException(401)

    settings = SimpleNamespace(market_service_url="http://127.0.0.1:18479", market_reader_token="internal", market_sync_token="replica")
    app = FastAPI()
    mount_market(app, settings, reader)
    return TestClient(app), calls, response, settings


def test_reader_proxy_is_bounded_and_does_not_expose_sync(market):
    client, calls, _, _ = market
    assert client.get("/v1/market/view").status_code == 401
    assert client.get("/v1/market/view", headers={"Authorization": "Bearer reader"}).json()["schema"] == 1
    assert str(calls[0].url).startswith("http://127.0.0.1:18479/view?")
    assert calls[0].headers["authorization"] == "Bearer internal"
    for path in ["/v1/market/sync/export", "/v1/market/sync/state?peer="+"a"*36]:
        assert client.get(path, headers={"Authorization": "Bearer reader"}).status_code == 401
    assert client.post("/v1/market/sync/import", json={}, headers={"Authorization": "Bearer reader"}).status_code == 401
    before = len(calls)
    for query in ["payment=shell", "hours=999", "smoothing=42", "interval=1s"]:
        assert client.get("/v1/market/view?"+query, headers={"Authorization": "Bearer reader"}).status_code == 422
    assert len(calls) == before


def test_sync_auth_separate_raw_body_preserved_and_limit(market):
    client, calls, _, _ = market
    headers = {"Authorization": "Bearer replica"}
    raw = b'{"schema":1,"records":[]}'
    assert client.post("/v1/market/sync/import", content=raw, headers=headers).status_code == 200
    assert calls[-1].content == raw
    assert calls[-1].headers["authorization"] == "Bearer replica"
    before = len(calls)
    assert client.post("/v1/market/sync/import", content=b"x"*4_000_001, headers=headers).status_code == 413
    assert len(calls) == before
    assert client.get("/v1/market/view", headers=headers).status_code == 401


def test_market_upstream_failures_and_unconfigured(market):
    client, calls, response, settings = market
    headers = {"Authorization": "Bearer reader"}
    response["status"] = 401
    assert client.get("/v1/market/view", headers=headers).status_code == 502
    response["status"] = 503
    response["body"] = {"detail": "busy"}
    assert client.get("/v1/market/view", headers=headers).json() == {"detail": "busy"}
    settings.market_service_url = ""
    settings.market_reader_token = ""
    before = len(calls)
    assert client.get("/v1/market/view", headers=headers).status_code == 503
    assert len(calls) == before


def test_versioned_market_proxy_keeps_filter_scope_and_private_cache(market):
    client, calls, response, _ = market
    assert client.get('/v1/market/view-update').status_code == 401
    revision = 'a' * 64
    response['body'] = {'protocol': 2, 'kind': 'unchanged', 'revision': revision}
    result = client.get('/v1/market/view-update?payment=bank&hours=12&since='+revision,
                        headers={'Authorization': 'Bearer reader'})
    assert result.json() == response['body']
    assert result.headers['cache-control'] == 'private, no-store'
    assert calls[-1].url.path == '/view-update'
    assert calls[-1].url.params['since'] == revision
    assert calls[-1].url.params['payment'] == 'bank'
    assert calls[-1].url.params['hours'] == '12'
    count = len(calls)
    assert client.get('/v1/market/view-update?since=bad', headers={'Authorization': 'Bearer reader'}).status_code == 422
    assert len(calls) == count
