from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from html import escape

import httpx
import pytest

from radar import industry_sources as mod


def rss_item(title="GPU order", link="https://nvidianews.nvidia.com/news/order", date=None, text="real text"):
    if date is None:
        date = format_datetime(datetime.now(UTC) - timedelta(hours=2))
    return (f"<item><title>{escape(title)}</title><link>{escape(link)}</link>"
            f"<guid>{escape(link)}</guid><pubDate>{escape(date)}</pubDate>"
            f"<description><![CDATA[{text}]]></description></item>")


def rss(*items):
    return '<rss version="2.0"><channel><title>Official</title>' + ''.join(items) + '</channel></rss>'


def client_for(handler, **kwargs):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


async def test_rss_html_dates_duplicates_and_scopes():
    body = rss(rss_item(text='<b>Revenue</b> 10<script>invented growth</script>'),
               rss_item(date=""), rss_item(link="https://attacker.test/x"),
               rss_item(link="https://nvidianews.nvidia.com/news/future",
                        date=format_datetime(datetime.now(UTC) + timedelta(days=1))),
               rss_item(link="https://nvidianews.nvidia.com/news/old",
                        date=format_datetime(datetime.now(UTC) - timedelta(days=500))),
               rss_item(title="duplicate"))
    async with client_for(lambda r: httpx.Response(200, text=body, headers={"content-type": "text/xml"})) as c:
        rows = await mod.fetch_source(c, mod.SOURCES["nvidia-news"])
    assert len(rows) == 1
    assert rows[0].text == "Revenue 10"
    assert rows[0].entity_ids == ["nvidia"]
    assert rows[0].published_at.endswith("Z")
    assert rows[0].metadata["evidence_scope"] == "feed_summary"
    assert rows[0].metadata["is_full_text"] is False


async def test_credentials_and_cookies_never_sent():
    def handler(request):
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert "x-api-key" not in request.headers
        return httpx.Response(200, text=rss(rss_item()), headers={"content-type": "text/xml"})
    async with client_for(handler, auth=("secret", "password"), cookies={"session": "secret"},
                          headers={"Authorization": "Bearer secret", "X-Api-Key": "secret"}) as c:
        assert await mod.fetch_source(c, mod.SOURCES["nvidia-news"])


@pytest.mark.parametrize("change", [{"url": "https://evil.test/"}, {"fee": "paid"}, {"id": "unknown"}])
async def test_only_exact_registry_source(change):
    async with client_for(lambda r: pytest.fail("untrusted request")) as c:
        with pytest.raises(mod.IndustrySourceError, match="registered"):
            await mod.fetch_source(c, replace(mod.SOURCES["nvidia-news"], **change))


@pytest.mark.parametrize("kwargs", [{"lookback_days": 0}, {"lookback_days": 366}, {"max_items": 31},
                                    {"max_items": False}])
async def test_option_bounds(kwargs):
    async with client_for(lambda r: pytest.fail("invalid options requested")) as c:
        with pytest.raises(mod.IndustrySourceError):
            await mod.fetch_source(c, mod.SOURCES["nvidia-news"], **kwargs)


@pytest.mark.parametrize("status,code", [(302,"upstream_error"),(403,"blocked"),(429,"rate_limited")])
async def test_failure_sanitized_and_no_redirect(status, code):
    calls = []
    def handler(r):
        calls.append(str(r.url))
        return httpx.Response(status, text="PRIVATE BODY", headers={"location": "https://evil.test/secret"})
    async with client_for(handler, follow_redirects=True) as c:
        with pytest.raises(mod.IndustrySourceError) as failure:
            await mod.fetch_source(c, mod.SOURCES["nvidia-news"])
    assert failure.value.code == code
    assert "PRIVATE" not in str(failure.value)
    assert len(calls) == 1


@pytest.mark.parametrize("body", ['<html>challenge</html>',
                                  '<!DOCTYPE rss [<!ENTITY evil "xx">]><rss version="2.0"/>'])
async def test_invalid_xml_or_challenge_rejected(body):
    async with client_for(lambda r: httpx.Response(200,text=body,headers={"content-type":"text/xml"})) as c:
        with pytest.raises(mod.IndustrySourceError):
            await mod.fetch_source(c, mod.SOURCES["nvidia-news"])


async def test_body_limit(monkeypatch):
    monkeypatch.setattr(mod, "MAX_RESPONSE_BYTES", 20)
    async with client_for(lambda r: httpx.Response(200, content=b'x' * 21,
                          headers={"content-type": "text/xml"})) as c:
        with pytest.raises(mod.IndustrySourceError) as failure:
            await mod.fetch_source(c, mod.SOURCES["nvidia-news"])
    assert failure.value.code == "too_large"


async def test_policy_local_filter_and_bounded_pagination():
    calls = []
    def handler(r):
        calls.append(str(r.url))
        assert r.url.host == "www.federalregister.gov"
        # Pretend search query was ignored. "chip" and lowercase "ai" must not admit unrelated rules.
        titles = ["Potato chip import duties", "Survey code ai", "Semiconductor export policy",
                  "Artificial intelligence governance", "Data-center development"]
        results = [{"title": t, "document_number": f"2026-0000{i}",
                    "publication_date": (datetime.now(UTC)-timedelta(days=1)).date().isoformat(),
                    "html_url": f"https://www.federalregister.gov/documents/2026/01/01/2026-0000{i}/rule",
                    "abstract": "Official abstract", "agencies": [{"name":"Commerce"}]} for i,t in enumerate(titles)]
        results += [{**results[2], "document_number":"2026-77777", "publication_date": ""},
                    {**results[2], "document_number":"2026-88888", "html_url":"https://evil.test/x"}]
        return httpx.Response(200,json={"results": results,"total_pages":100,
                                       "next_page_url":"https://evil.test/secret"})
    async with client_for(handler) as c:
        rows=await mod.fetch_source(c,mod.SOURCES["federal-register-ai"])
    assert len(calls) == 2
    assert len(rows) == 3
    assert all(r.published_precision == "date" for r in rows)
    assert all(r.metadata["evidence_scope"] == "policy_abstract" for r in rows)
    assert all(r.metadata["local_topic_filter"] for r in rows)


def sec_payload(document="nvda.htm", cik="1045810"):
    date=datetime.now(UTC)-timedelta(days=2)
    return {"cik":cik,"filings":{"recent":{
        "form":["8-K"],"filingDate":[date.date().isoformat()],
        "acceptanceDateTime":[date.isoformat().replace("+00:00","Z")],
        "accessionNumber":["0001045810-26-000078"],"primaryDocument":[document]}}}


async def test_sec_archive_block_does_not_turn_metadata_into_body(monkeypatch):
    monkeypatch.setattr(mod,"_SEC_NEXT",0)
    def handler(r):
        if r.url.host == "data.sec.gov":
            return httpx.Response(200,json=sec_payload())
        assert str(r.url) == "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000078/nvda.htm"
        return httpx.Response(403,text="secret upstream error")
    async with client_for(handler) as c:
        rows=await mod.fetch_source(c,mod.SOURCES["sec-nvidia"])
    assert rows[0].kind == "filing_notice"
    assert rows[0].metadata["document_fetch"] == "unavailable"
    assert rows[0].text.startswith("SEC filing notice:")
    assert "secret" not in rows[0].text


async def test_sec_cover_only_is_notice(monkeypatch):
    monkeypatch.setattr(mod,"_SEC_NEXT",0)
    def handler(r):
        if r.url.host == "data.sec.gov":
            return httpx.Response(200,json=sec_payload())
        return httpx.Response(200,text='<html><p>' + 'SEC cover ' * 100 +
                              'furnished as exhibit 99.1</p></html>',headers={"content-type":"text/html"})
    async with client_for(handler) as c:
        rows=await mod.fetch_source(c,mod.SOURCES["sec-nvidia"])
    assert rows[0].kind == "filing_notice"
    assert rows[0].metadata["document_fetch"] == "cover_only"


async def test_sec_full_primary_document_keeps_scope(monkeypatch):
    monkeypatch.setattr(mod,"_SEC_NEXT",0)
    def handler(r):
        if r.url.host == "data.sec.gov":
            return httpx.Response(200,json=sec_payload())
        return httpx.Response(200,text='<html><p>' + 'Capital expenditure grew. ' * 400 +
                              '</p></html>',headers={"content-type":"text/html"})
    async with client_for(handler) as c:
        rows=await mod.fetch_source(c,mod.SOURCES["sec-nvidia"])
    assert rows[0].kind == "filing_document"
    assert rows[0].metadata["includes_exhibits"] is False
    assert rows[0].metadata["evidence_scope"] == "filing_primary_document"


@pytest.mark.parametrize("document",["https://evil.test/a.htm","../a.htm","%2e%2e/a.htm","a.htm?secret=x"])
async def test_sec_untrusted_document_not_requested(document,monkeypatch):
    monkeypatch.setattr(mod,"_SEC_NEXT",0)
    def handler(r):
        assert r.url.host == "data.sec.gov"
        return httpx.Response(200,json=sec_payload(document))
    async with client_for(handler) as c:
        assert not await mod.fetch_source(c,mod.SOURCES["sec-nvidia"])


async def test_sec_mismatched_cik_rejected(monkeypatch):
    monkeypatch.setattr(mod,"_SEC_NEXT",0)
    async with client_for(lambda r:httpx.Response(200,json=sec_payload(cik="1"))) as c:
        with pytest.raises(mod.IndustrySourceError,match="identity mismatch"):
            await mod.fetch_source(c,mod.SOURCES["sec-nvidia"])


def test_registry_complete_free_and_entity_references():
    assert 20 <= len(mod.ENTITIES) <= 30
    assert {t for e in mod.ENTITIES.values() for t in e["themes"]} == {
        "infrastructure","software","workflows"}
    assert all(s.fee == "free" and s.refresh_minutes >= 30 for s in mod.SOURCES.values())
    assert all(e in mod.ENTITIES for s in mod.SOURCES.values() for e in s.entity_ids)


@pytest.mark.parametrize("value",["",None,"yesterday","2026-08-01T12:00:00"])
def test_missing_or_ambiguous_publication_date_rejected(value):
    with pytest.raises((ValueError,TypeError)):
        mod._date(value)


async def test_empty_content_uses_summary_and_enriches_company_body():
    body = rss(rss_item(text='Original excerpt')).replace('</item>',
            '<content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/"></content:encoded></item>')
    calls=[]
    def handler(r):
        calls.append(str(r.url))
        if str(r.url) == mod.SOURCES['nvidia-news'].url:
            return httpx.Response(200,text=body,headers={'content-type':'text/xml'})
        return httpx.Response(200,text='<div class="article-body"><p>' +
                              'Revenue outlook and customer deployment. ' * 30 +
                              '</p><script>bad source</script></div>',headers={'content-type':'text/html'})
    async with client_for(handler) as c:
        rows=await mod.fetch_source(c,mod.SOURCES['nvidia-news'])
    assert len(calls) == 2
    assert rows[0].metadata['evidence_scope'] == 'publisher_article'
    assert rows[0].metadata['is_full_text']
    assert 'bad source' not in rows[0].text
    assert 'Revenue outlook' in rows[0].text


async def test_article_download_failure_keeps_feed_excerpt():
    body = rss(rss_item(text='Original excerpt')).replace('</item>',
            '<content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/"></content:encoded></item>')
    def handler(r):
        if str(r.url) == mod.SOURCES['nvidia-news'].url:
            return httpx.Response(200,text=body,headers={'content-type':'text/xml'})
        return httpx.Response(403,text='secret challenge')
    async with client_for(handler) as c:
        rows=await mod.fetch_source(c,mod.SOURCES['nvidia-news'])
    assert rows[0].text == 'Original excerpt'
    assert rows[0].metadata['article_fetch'] == 'unavailable'
    assert rows[0].metadata['partial'] is True


async def test_article_reads_capped_at_three_and_fixed_path():
    body = rss(*(rss_item(link=f'https://nvidianews.nvidia.com/news/{i}') for i in range(8)))
    calls=[]
    def handler(r):
        calls.append(str(r.url))
        if str(r.url) == mod.SOURCES['nvidia-news'].url:
            return httpx.Response(200,text=body,headers={'content-type':'text/xml'})
        return httpx.Response(403)
    async with client_for(handler) as c:
        rows=await mod.fetch_source(c,mod.SOURCES['nvidia-news'])
    assert len(rows) == 8
    assert len(calls) == 4


async def test_feed_item_cannot_choose_non_article_get_path():
    calls=[]
    body = rss(rss_item(link='https://nvidianews.nvidia.com/logout'))
    def handler(r):
        calls.append(str(r.url))
        return httpx.Response(200,text=body,headers={'content-type':'text/xml'})
    async with client_for(handler) as c:
        rows=await mod.fetch_source(c,mod.SOURCES['nvidia-news'])
    assert len(calls) == 1
    assert rows[0].metadata['article_fetch'] == 'unavailable'
