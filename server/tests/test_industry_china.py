from datetime import UTC, datetime, timedelta

import httpx
import pytest

from radar import industry_china as cn
from radar import industry_sources as sources


def announcement(**change):
    stamp = int((datetime.now(UTC) - timedelta(days=1)).timestamp() * 1000)
    return {
        "secCode": "000977",
        "orgId": "gssz0000977",
        "announcementTitle": "2026年半年度报告全文",
        "announcementTime": stamp,
        "adjunctUrl": "finalpage/2026-09-12/123456.PDF",
        **change,
    }


async def test_identity_dates_topics_and_pdf_fallback():
    calls = []

    def handle(r):
        calls.append(r)
        assert "authorization" not in r.headers and "cookie" not in r.headers and "x-api-key" not in r.headers
        if r.url.host == "static.cninfo.com.cn":
            return httpx.Response(403, text="upstream secret")
        return httpx.Response(
            200,
            json={
                "totalAnnouncement": 5,
                "announcements": [
                    announcement(),
                    announcement(secCode="002230"),
                    announcement(announcementTime=None),
                    announcement(adjunctUrl="https://evil.test/test.pdf"),
                    announcement(announcementTitle="公司章程"),
                ],
                "hasMore": False,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        auth=("secret", "secret"),
        cookies={"secret": "secret"},
        headers={"X-Api-Key": "secret"},
    ) as client:
        rows = await sources.fetch_source(client, sources.SOURCES["cninfo-inspur"])
    assert len(rows) == 1 and rows[0].kind == "filing_notice"
    assert rows[0].metadata["document_fetch"] == "unavailable"
    assert rows[0].published_precision == "date" and rows[0].published_at.endswith("+00:00")
    assert rows[0].entity_ids == ["inspur"]
    assert len(calls) == 2
    assert "gssz0000977" in calls[0].content.decode()


async def test_full_disclosure_and_pdf_budgets(monkeypatch):
    seen = []

    async def extract(body, ct, url):
        seen.append(url)
        return {"text": "经营报告原始正文" * 100, "partial": True}

    monkeypatch.setattr(cn, "extract_page", extract)

    def handle(r):
        if r.url.host == "static.cninfo.com.cn":
            return httpx.Response(200, content=b"%PDF-test", headers={"content-type": "application/pdf"})
        return httpx.Response(
            200,
            json={
                "totalAnnouncement": 8,
                "announcements": [announcement(adjunctUrl=f"finalpage/2026-09-12/{i}.PDF") for i in range(8)],
                "hasMore": False,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        rows = await sources.fetch_source(client, sources.SOURCES["cninfo-inspur"])
    assert len(rows) == 8 and len(seen) == 2
    assert sum(r.kind == "filing_document" for r in rows) == 2
    assert all(r.metadata["partial"] for r in rows)


async def test_alibaba_identity_path_and_visible_date():
    day = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y年%m月%d日")
    html = f'<div class="filings if-pc"><div class="filings-title"><a href="https://data.alibabagroup.com/ir_filings/HKEX/09988/cn/test/test.pdf">季度業績公告</a></div><div class="filings-date">{day}</div></div>'

    def handle(r):
        return (
            httpx.Response(200, text=html, headers={"content-type": "text/html"})
            if r.url.host == "www.alibabagroup.com"
            else httpx.Response(403)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
        rows = await sources.fetch_source(c, sources.SOURCES["alibaba-hk"])
    assert rows[0].entity_ids == ["alibaba-cn"] and rows[0].metadata["region"] == "cn"


async def test_policy_topic_dates_and_direct_only(monkeypatch):
    day = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y/%m/%d")
    html = f'<ul><li><a href="./202609/test.html">新型电力系统规划</a><span>{day}</span></li><li><a href="./202609/other.html">体育公园通知</a><span>{day}</span></li><li><a href="./202609/undated.html">人工智能通知</a></li></ul>'
    urls = []

    def handle(r):
        urls.append(str(r.url))
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    async def extract(*a):
        return {
            "text": "政策正文不得当成收入改善" * 80,
            "partial": False,
            "links": [{"url": "https://evil.test"}],
        }

    monkeypatch.setattr(cn, "extract_page", extract)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
        rows = await sources.fetch_source(c, sources.SOURCES["ndrc-policy"])
    assert len(rows) == 1 and rows[0].kind == "policy" and len(urls) == 2
    assert all("evil" not in u for u in urls)


@pytest.mark.parametrize("status", [302, 403, 429])
async def test_no_redirect_no_auth_bypass(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status, text="secret", headers={"location": "https://evil.test"})
        )
    ) as c:
        with pytest.raises(sources.IndustrySourceError) as exc:
            await sources.fetch_source(c, sources.SOURCES["cninfo-inspur"])
    assert "secret" not in str(exc.value)
