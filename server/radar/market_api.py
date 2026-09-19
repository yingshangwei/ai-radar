"""Authenticated adapter to the separately deployed, private TideWatch service."""
import secrets
from typing import Literal

import httpx
from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response

MAX_SYNC_BODY = 4_000_000


def mount_market(app, settings, authenticated):
    base = settings.market_service_url.rstrip("/")

    async def forward(path, *, params=None, body=None, sync=False):
        token = settings.market_sync_token if sync else settings.market_reader_token
        if not base or not token:
            raise HTTPException(503, "行情服务尚未配置")
        try:
            async with httpx.AsyncClient(timeout=65, trust_env=False, follow_redirects=False) as client:
                async with client.stream(
                    "POST" if body is not None else "GET", base + path,
                    params=params, content=body,
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                ) as upstream:
                    if upstream.status_code not in (200, 409, 413, 422, 503):
                        raise HTTPException(502, "行情服务响应异常")
                    data = bytearray()
                    async for chunk in upstream.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 8_000_000:
                            raise HTTPException(502, "行情响应超过限制")
                    return Response(bytes(data), status_code=upstream.status_code, media_type="application/json")
        except httpx.HTTPError:
            raise HTTPException(503, "行情服务暂时不可用，请稍后刷新") from None

    def sync_auth(authorization: str | None = Header(default=None)):
        if not settings.market_sync_token or not secrets.compare_digest(
            authorization or "", "Bearer " + settings.market_sync_token
        ):
            raise HTTPException(401, "需要独立的数据同步令牌")

    @app.get("/v1/market/view-update", dependencies=[Depends(authenticated)])
    @app.get("/v1/market/view", dependencies=[Depends(authenticated)])
    async def view(
        request: Request,
        since: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$"),
        payment: Literal["bank", "alipay", "merged"] = "merged",
        ticket: int = Query(default=10000, ge=0, le=100_000_000),
        mode: Literal["strict", "rateOnly"] = "strict",
        hours: int = Query(default=24, ge=1, le=168),
        interval: Literal["1m", "5m", "15m", "1h", "1d", "7d"] = "5m",
        smoothing: int = Query(default=300, ge=0, le=900),
        end: float | None = Query(default=None, gt=0),
    ):
        if smoothing not in (0, 300, 900):
            raise HTTPException(422, "平滑周期无效")
        params = dict(payment=payment, ticket=ticket, mode=mode, hours=hours, interval=interval, smoothing=smoothing)
        if end is not None:
            params["end"] = end
        if request.url.path.endswith("view-update"):
            if since is not None:
                params["since"] = since
            response = await forward("/view-update", params=params)
            response.headers["Cache-Control"] = "private, no-store"
            return response
        return await forward("/view", params=params)

    @app.get("/v1/market/sync/state", dependencies=[Depends(sync_auth)])
    async def sync_state(peer: str = Query(pattern=r"^[A-Fa-f0-9-]{36}$")):
        return await forward("/sync/state", params={"peer": peer}, sync=True)

    @app.get("/v1/market/sync/export", dependencies=[Depends(sync_auth)])
    async def sync_export(after: int = Query(default=0, ge=0), limit: int = Query(default=500, ge=1, le=1000)):
        return await forward("/sync/export", params={"after": after, "limit": limit}, sync=True)

    @app.post("/v1/market/sync/import", dependencies=[Depends(sync_auth)])
    async def sync_import(request: Request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_SYNC_BODY:
                raise HTTPException(413, "同步批次超过限制")
        return await forward("/sync/import", body=bytes(data), sync=True)
