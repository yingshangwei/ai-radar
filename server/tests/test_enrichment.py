import httpx
import pytest

from radar.enrichment import allowed, enrich


@pytest.mark.parametrize(
    "url",
    [
        "http://openai.com/a",
        "https://127.0.0.1/a",
        "https://openai.com.evil.org/a",
        "https://user:password@openai.com/a",
        "https://openai.com:8000/a",
    ],
)
def test_only_explicit_publisher_origins_are_allowed(url):
    assert not allowed(url)


@pytest.mark.asyncio
async def test_untrusted_import_url_is_not_fetched(respx_mock):
    article = {"id": "a", "url": "http://127.0.0.1/admin", "text": "AI excerpt"}
    assert (await enrich([article]))[0]["text"] == "AI excerpt"
    assert not respx_mock.calls


@pytest.mark.asyncio
async def test_redirect_cannot_escape_publisher_allowlist(respx_mock):
    route = respx_mock.get("https://openai.com/test").mock(
        return_value=httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
    )
    result = await enrich([{"id": "a", "url": "https://openai.com/test", "text": "Original excerpt"}])
    assert result[0]["text"] == "Original excerpt" and route.call_count == 1 and len(respx_mock.calls) == 1


@pytest.mark.asyncio
async def test_publisher_body_enriches_digest_evidence(respx_mock):
    html = (
        "<html><body><article><h1>AI model release</h1><p>"
        + ("This is a detailed AI model research announcement with actual evidence and results. " * 20)
        + "</p></article></body></html>"
    )
    respx_mock.get("https://openai.com/test").mock(
        return_value=httpx.Response(200, text=html, headers={"Content-Type": "text/html"})
    )
    result = await enrich([{"id": "a", "url": "https://openai.com/test", "text": "Short headline"}])
    assert result[0]["evidence_type"] == "publisher_article"
    assert len(result[0]["text"]) > 200
