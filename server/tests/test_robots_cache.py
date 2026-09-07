import asyncio

import httpx
import pytest

from radar.web_reader import PageFetcher, PageUnavailable


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def addresses(host, port):
        return ["93.184.216.34"]
    monkeypatch.setattr("radar.web_reader.public_addresses", addresses)


@pytest.mark.parametrize("status,expected", [(401, "auth_required"), (403, "access_restricted"),
                                             (429, "rate_limited"), (503, "unavailable")])
async def test_concurrent_failed_robots_only_hits_origin_once(respx_mock, status, expected):
    async def response(request):
        await asyncio.sleep(0.01)
        return httpx.Response(status)
    route = respx_mock.get("https://93.184.216.34/robots.txt").mock(side_effect=response)
    fetcher = PageFetcher()
    results = await asyncio.gather(*(fetcher.check_robots(f"https://example.org/article-{i}")
                                     for i in range(19)), return_exceptions=True)
    assert route.call_count == 1
    assert all(isinstance(error, PageUnavailable) and error.status == expected for error in results)
    assert len({id(error) for error in results}) == len(results)
    assert fetcher.robots_errors["https://example.org"] == (expected, results[0].message)
    assert "https://example.org" not in fetcher.robots


async def test_failed_rules_cached_but_retry_next_collection(respx_mock):
    route = respx_mock.get("https://93.184.216.34/robots.txt").mock(side_effect=httpx.ReadTimeout("timeout"))
    fetcher = PageFetcher()
    for _ in range(2):
        with pytest.raises(PageUnavailable) as error:
            await fetcher.check_robots("https://example.org/article")
        assert error.value.status == "unavailable"
    assert route.call_count == 1
    route.respond(200, text="User-agent: *\nAllow: /\n")
    await PageFetcher().check_robots("https://example.org/article")
    assert route.call_count == 2


@pytest.mark.parametrize("status", [404, 410])
async def test_missing_robots_allows_pages_and_is_cached(respx_mock, status):
    route = respx_mock.get("https://93.184.216.34/robots.txt").respond(status)
    fetcher = PageFetcher()
    await asyncio.gather(*(fetcher.check_robots(f"https://example.org/article-{i}") for i in range(5)))
    assert route.call_count == 1 and not fetcher.robots_errors


async def test_success_rules_remain_per_path_and_origin(respx_mock):
    async def response(request):
        await asyncio.sleep(0.01)
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    route = respx_mock.get("https://93.184.216.34/robots.txt").mock(side_effect=response)
    fetcher = PageFetcher()
    await asyncio.gather(*(fetcher.check_robots(f"https://example.org/article-{i}") for i in range(5)))
    with pytest.raises(PageUnavailable) as error:
        await fetcher.check_robots("https://example.org/private")
    assert error.value.status == "restricted" and route.call_count == 1
    await fetcher.check_robots("https://another.example/article")
    assert route.call_count == 2 and not fetcher.robots_errors


async def test_cancelled_lookup_not_cached_and_releases_waiter(respx_mock):
    entered = asyncio.Event()
    request_count = 0
    async def response(request):
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")
    route = respx_mock.get("https://93.184.216.34/robots.txt").mock(side_effect=response)
    fetcher = PageFetcher()
    initial = asyncio.create_task(fetcher.check_robots("https://example.org/article"))
    await entered.wait()
    waiter = asyncio.create_task(fetcher.check_robots("https://example.org/other"))
    initial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await initial
    await asyncio.wait_for(waiter, timeout=1)
    # RESPX records completed calls; the cancelled transport attempt is omitted.
    assert request_count == 2 and route.call_count == 1 and not fetcher.robots_errors
