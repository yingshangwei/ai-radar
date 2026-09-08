"""Readiness and browser reuse; optional real Chromium uses an isolated fixture."""

import asyncio
import os
from types import SimpleNamespace

import httpx
import pytest

from radar import browser_readiness, browser_worker
from radar.browser_worker import Browser


class Clock:
    value = 0.0

    def now(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds


async def test_readiness_waits_for_delayed_article_without_following_links(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(browser_readiness.time, "monotonic", clock.now)
    monkeypatch.setattr(browser_readiness.asyncio, "sleep", clock.sleep)

    class Page:
        async def evaluate(self, script):
            assert script == browser_readiness.SNAPSHOT
            return {"ready": clock.value >= 4, "signature": "loaded" if clock.value >= 4 else "loading"}

    result = await browser_readiness.wait_for_content(Page())
    assert not result["timed_out"] and 5.2 <= clock.value < 7


async def test_continuously_changing_page_has_bounded_timeout_and_scrolls(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(browser_readiness.time, "monotonic", clock.now)
    monkeypatch.setattr(browser_readiness.asyncio, "sleep", clock.sleep)

    class Page:
        scrolls = 0

        async def evaluate(self, script):
            if script != browser_readiness.SNAPSHOT:
                self.scrolls += 1
                assert "scrollBy" in script and "click" not in script
                return
            return {"ready": True, "signature": str(clock.value), "height": 10000, "viewport": 760}

    page = Page()
    assert (await browser_readiness.wait_for_content(page))["timed_out"]
    assert page.scrolls == 3 and clock.value < 13


async def test_challenge_stops_readiness_without_scrolling_or_retry(monkeypatch):
    class Page:
        async def evaluate(self, script):
            assert script == browser_readiness.SNAPSHOT
            return {"blocked": True}

    assert await browser_readiness.wait_for_content(Page()) == {"blocked": True, "timed_out": False}


async def test_blocked_javascript_cannot_hold_readiness_lock_forever():
    cancelled = []

    class Page:
        async def evaluate(self, script):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    assert (await browser_readiness.wait_for_content(Page(), timeout_seconds=0.02))["timed_out"]
    assert cancelled == [True]


def test_reuse_is_limited_to_same_domain_idle_background_context(monkeypatch):
    monkeypatch.setattr(browser_worker.time, "monotonic", lambda: 100)
    browser = Browser()
    browser.context = object()
    browser.page = SimpleNamespace(is_closed=lambda: False)
    browser.background_domain, browser.background_until, browser.background_uses = "example.org", 150, 1
    assert browser.can_reuse("example.org")
    assert not browser.can_reuse("other.example.org")
    assert not browser.can_reuse("example.org", interactive=True)
    browser.active = {"id": "interactive"}
    assert not browser.can_reuse("example.org")
    browser.active = None
    browser.background_uses = 8
    assert not browser.can_reuse("example.org")
    browser.background_uses, browser.background_until = 1, 99
    assert not browser.can_reuse("example.org")


@pytest.mark.parametrize("outcome", ["fetched", "access_restricted", "exception", "page_unavailable"])
async def test_fetch_retains_only_successful_background_browser(monkeypatch, outcome):
    browser = Browser()
    closed = []

    async def launch(url, interactive):
        assert not interactive
        return url

    async def capture(url):
        if outcome == "exception":
            raise RuntimeError("synthetic failure")
        if outcome == "page_unavailable":
            raise browser_worker.PageUnavailable("restricted", "Synthetic robots restriction")
        return {"status": outcome}

    async def close():
        closed.append(True)

    browser.launch, browser.capture, browser.close = launch, capture, close
    monkeypatch.setattr(browser_worker, "Browser", lambda: browser)
    monkeypatch.setenv("RADAR_BROWSER_WORKER_TOKEN", "w" * 32)
    app = browser_worker.create_worker()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/fetch", json={"url": "https://example.org/article"},
            headers={"Authorization": "Bearer " + "w" * 32})
    assert response.status_code == 200
    assert bool(closed) == (outcome != "fetched")


async def test_hung_interactive_capture_closes_and_releases_queue(monkeypatch):
    browser = Browser()
    browser.active = {"id": "fixture", "url": "https://example.org/article",
                      "expires": browser_worker.time.time() + 1200}
    closed = []
    real_timeout = asyncio.timeout

    async def capture(url):
        await asyncio.Event().wait()

    async def close():
        browser.active = None
        closed.append(True)

    browser.capture, browser.close = capture, close
    monkeypatch.setattr(browser_worker, "Browser", lambda: browser)
    monkeypatch.setattr(browser_worker.asyncio, "timeout", lambda seconds: real_timeout(0.02))
    monkeypatch.setenv("RADAR_BROWSER_WORKER_TOKEN", "w" * 32)
    app = browser_worker.create_worker()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/sessions/fixture/capture",
            headers={"Authorization": "Bearer " + "w" * 32})
    assert response.json()["status"] == "unavailable"
    assert closed == [True] and browser.active is None and not browser.lock.locked()


@pytest.mark.skipif(not os.environ.get("RADAR_TEST_CHROMIUM"), reason="Explicit isolated Chromium fixture required")
async def test_real_chromium_waits_for_article_inserted_after_old_fixed_delay():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=os.environ["RADAR_TEST_CHROMIUM"])
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content("""<main aria-busy="true"><p>This is a long placeholder that must never be considered complete article evidence while the container remains busy loading its actual original contents.</p></main><script>
              setTimeout(() => {document.querySelector('main').innerHTML = '<article><h1>Delayed research</h1>'
                + '<p>The complete original article is now available with reproducible evaluation methods and detailed limitations.</p>'
                + '<pre><code>score = evaluate(model)</code></pre></article>';
                document.querySelector('main').removeAttribute('aria-busy');}, 3300);
            </script>""")
            result = await browser_readiness.wait_for_content(page)
            assert not result["timed_out"]
            assert "complete original article" in await page.locator("article").inner_text()
            assert await page.locator("code").inner_text() == "score = evaluate(model)"
        finally:
            await browser.close()
