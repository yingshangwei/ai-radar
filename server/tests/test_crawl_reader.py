"""Captured HTML fidelity, bounded extraction, and no extra network behavior."""
import asyncio
import importlib.metadata
import json

import pytest

from radar import crawl_reader

URL = "https://example.org/research/article"
ARTICLE = b"""<!doctype html><html><head><title>Research result</title></head><body>
<nav><a href='/unrelated'>Site navigation should disappear</a></nav>
<main><article><h1>Research result</h1>
<p>The experiment measures attention efficiency under a fixed compute budget.
Its new architecture preserves quality while reducing memory consumption.</p>
<h2>Measured results</h2><table><tr><th>Model</th><th>Accuracy</th></tr>
<tr><td>Baseline</td><td>91.2%</td></tr><tr><td>Proposed</td><td>92.7%</td></tr></table>
<pre><code>def attention(x):\n    return x @ x.T</code></pre>
<p>Read the <a href='/research/paper?utm_source=test'>original paper</a> and
<a href='https://github.com/example/research'>implementation</a>.</p>
<p hidden>Private hidden material must not become evidence.</p>
<script>SECRET_SCRIPT_TEXT()</script><iframe src='http://127.0.0.1/private'></iframe>
<img src='http://169.254.169.254/metadata' alt='remote metadata must not be fetched'>
<form><p>Sign in using your password.</p><input type='password'></form>
</article></main><footer><a href='/terms'>Terms</a> unrelated footer text</footer>
</body></html>"""


def require_crawl4ai():
    try:
        version = importlib.metadata.version("crawl4ai")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("run extraction fidelity tests with the optional browser dependencies")
    assert version == crawl_reader.CRAWL4AI_VERSION


@pytest.mark.parametrize("url", ["file:///etc/passwd", "raw:<html>secret</html>",
                                 "http://user:password@example.org/", "https://example.org:8443/"])
async def test_non_public_protocols_and_credentials_never_reach_parser(monkeypatch, url):
    async def unexpected(*_):
        pytest.fail("invalid input must not invoke either parser")
    monkeypatch.setattr(crawl_reader, "_extract_isolated", unexpected)
    monkeypatch.setattr(crawl_reader, "extract_page", unexpected)
    with pytest.raises(ValueError):
        await crawl_reader.extract_crawl_page(ARTICLE, url)


@pytest.mark.parametrize("data", [b"x" * (crawl_reader.MAX_HTML + 1), b" %PDF-1.7 remote document"])
async def test_size_and_pdf_rejections_do_not_fallback(monkeypatch, data):
    async def unexpected(*_):
        pytest.fail("input rejection cannot expand into another parser")
    monkeypatch.setattr(crawl_reader, "_extract_isolated", unexpected)
    monkeypatch.setattr(crawl_reader, "extract_page", unexpected)
    with pytest.raises(ValueError):
        await crawl_reader.extract_crawl_page(data, URL)


def test_unreviewed_crawl4ai_version_is_not_loaded(monkeypatch):
    monkeypatch.setattr(crawl_reader.importlib.metadata, "version", lambda _: "0.9.2")
    with pytest.raises(crawl_reader.CrawlExtractionError, match="dependency_version_mismatch"):
        crawl_reader.parse_crawl_page(ARTICLE, URL)


@pytest.mark.parametrize("error,reason", [
    (crawl_reader.CrawlExtractionError("dependency_missing"), "dependency_missing"),
    (TimeoutError(), "extraction_timeout"), (OSError(), "parser_unavailable"),
])
async def test_fallback_is_visible_and_keeps_same_document(monkeypatch, error, reason):
    async def failed(data, url):
        assert data == ARTICLE and url == URL
        raise error
    async def fallback(data, content_type, url):
        assert (data, content_type, url) == (ARTICLE, "text/html", URL)
        return {"text": "Original source. " * 10, "title": "Source", "links": [], "partial": False}
    monkeypatch.setattr(crawl_reader, "_extract_isolated", failed)
    monkeypatch.setattr(crawl_reader, "extract_page", fallback)
    result = await crawl_reader.extract_crawl_page(ARTICLE, URL)
    assert result["extraction_engine"] == "trafilatura_fallback"
    assert result["extraction_fallback_reason"] == reason
    assert result["text"] == "Original source. " * 10


async def test_cancellation_kills_and_reaps_before_returning_without_fallback(monkeypatch):
    events = []
    class Process:
        async def communicate(self, _):
            raise asyncio.CancelledError
        def kill(self):
            events.append("killed")
        async def wait(self):
            events.append("reaped")
    async def spawn(*args, **kwargs):
        return Process()
    async def fallback(*_):
        pytest.fail("cancelled extraction must not start another parser")
    monkeypatch.setattr(crawl_reader.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(crawl_reader, "extract_page", fallback)
    with pytest.raises(asyncio.CancelledError):
        await crawl_reader.extract_crawl_page(ARTICLE, URL)
    assert events == ["killed", "reaped"]


async def test_parser_timeout_reaps_before_starting_fallback(monkeypatch):
    events = []
    class Process:
        async def communicate(self, _):
            await asyncio.Event().wait()
        def kill(self):
            events.append("killed")
        async def wait(self):
            events.append("reaped")
    async def spawn(*args, **kwargs):
        return Process()
    async def fallback(*_):
        assert events == ["killed", "reaped"]
        events.append("fallback")
        return {"text": "Evidence. " * 10}
    monkeypatch.setattr(crawl_reader, "PARSER_TIMEOUT", 0.01)
    monkeypatch.setattr(crawl_reader.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(crawl_reader, "extract_page", fallback)
    result = await crawl_reader.extract_crawl_page(ARTICLE, URL)
    assert result["extraction_fallback_reason"] == "extraction_timeout"
    assert events == ["killed", "reaped", "fallback"]


async def test_fallback_has_its_own_remaining_deadline(monkeypatch):
    cancelled = []
    async def unavailable(*_):
        raise crawl_reader.CrawlExtractionError("extraction_failed")
    async def slow_fallback(*_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr(crawl_reader, "FALLBACK_TIMEOUT", 0.01)
    monkeypatch.setattr(crawl_reader, "_extract_isolated", unavailable)
    monkeypatch.setattr(crawl_reader, "extract_page", slow_fallback)
    with pytest.raises(TimeoutError):
        await crawl_reader.extract_crawl_page(ARTICLE, URL)
    assert cancelled == [True]


async def test_child_environment_cannot_inherit_worker_or_model_credentials(monkeypatch):
    child = {}
    monkeypatch.setenv("RADAR_BROWSER_WORKER_TOKEN", "private-worker-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-model-key")
    document = {"text": "Evidence. " * 10, "links": [], "extraction_engine": "crawl4ai"}
    class Process:
        returncode = 0
        async def communicate(self, data):
            assert data == ARTICLE
            return json.dumps(document).encode(), b""
    async def spawn(*args, **kwargs):
        child.update(kwargs)
        return Process()
    monkeypatch.setattr(crawl_reader.asyncio, "create_subprocess_exec", spawn)
    assert await crawl_reader._extract_isolated(ARTICLE, URL) == document
    assert "RADAR_BROWSER_WORKER_TOKEN" not in child["env"]
    assert "DEEPSEEK_API_KEY" not in child["env"]
    assert child["env"]["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert child["env"]["PYTHON_DOTENV_DISABLED"] == "1"
    assert child["cwd"] == child["env"]["HOME"]


@pytest.mark.parametrize("event", ["socket.__new__", "socket.getaddrinfo", "socket.connect",
                                  "subprocess.Popen", "os.system", "os.posix_spawn", "os.fork"])
def test_extraction_audit_hook_rejects_network_and_child_processes(event):
    with pytest.raises(PermissionError):
        crawl_reader._deny_network_and_processes(event, ())
    crawl_reader._deny_network_and_processes("import", ())


async def test_real_package_preserves_structure_and_direct_links_without_network():
    require_crawl4ai()
    result = await crawl_reader._extract_isolated(ARTICLE, URL)
    assert result["extraction_engine"] == "crawl4ai"
    assert result["title"] == "Research result"
    assert "# Research result" in result["text"]
    assert "## Measured results" in result["text"]
    assert "91.2%" in result["text"] and "92.7%" in result["text"]
    assert "def attention(x):" in result["text"] and "return x @ x.T" in result["text"]
    assert [link["url"] for link in result["links"]] == [
        "https://example.org/research/paper", "https://github.com/example/research"]
    for excluded in ["SECRET_SCRIPT_TEXT", "Site navigation", "Private hidden", "password", "footer text"]:
        assert excluded not in result["text"]


async def test_real_package_bounds_long_document_and_link_count():
    require_crawl4ai()
    data = ("<html><title>Long article</title><body><main>" + "<p>AI result and exact evidence.</p>" * 3000
            + "".join(f'<a href="/paper/{i}">Paper {i}</a>' for i in range(60)) + "</main></body></html>").encode()
    result = await crawl_reader._extract_isolated(data, URL)
    assert len(result["text"]) == crawl_reader.MAX_TEXT and result["partial"] is True
    assert len(result["links"]) == 30


async def test_real_package_does_not_fetch_untrusted_base_or_pdf_reference():
    require_crawl4ai()
    data = ARTICLE.replace(b"<head>", b'<head><base href="http://127.0.0.1/">').replace(
        b"/research/paper?utm_source=test", b"/paper.pdf")
    result = await crawl_reader._extract_isolated(data, URL)
    # Preserve the actual reference, without fetching it. The downstream fetcher
    # separately checks DNS/public addresses before reading any source reference.
    assert result["links"][0]["url"] == "http://127.0.0.1/paper.pdf"
    assert result["title"] == "Research result"
    assert "169.254.169.254" not in result["text"]


@pytest.mark.parametrize("base,expected", [
    ("https://cdn.example.org/published/", "https://cdn.example.org/published/paper.pdf"),
    ("/published/", "https://example.org/published/paper.pdf"),
    ("file:///private/", None), ("https://user:password@other.example/", None),
])
async def test_real_package_base_keeps_reference_semantics_without_changing_document(base, expected):
    require_crawl4ai()
    data = ARTICLE.replace(b"<head>", f'<head><base href="{base}">'.encode()).replace(
        b"/research/paper?utm_source=test", b"paper.pdf")
    result = await crawl_reader._extract_isolated(data, URL)
    assert result["title"] == "Research result"
    assert result.get("url") is None  # Caller retains the verified canonical article URL.
    links = [link["url"] for link in result["links"]]
    assert links == ([expected] if expected else []) + ["https://github.com/example/research"]


async def test_real_package_ignores_short_login_document():
    require_crawl4ai()
    with pytest.raises(crawl_reader.CrawlExtractionError, match="no_readable_body"):
        await crawl_reader._extract_isolated(b"<html><title>Login</title><form>Sign in</form></html>", URL)


async def test_missing_optional_package_uses_real_legacy_parser():
    try:
        importlib.metadata.version("crawl4ai")
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        pytest.skip("checks the main API environment without optional browser dependencies")
    result = await crawl_reader.extract_crawl_page(ARTICLE, URL)
    assert result["extraction_engine"] == "trafilatura_fallback"
    assert result["extraction_fallback_reason"] == "dependency_missing"
    assert "fixed compute budget" in result["text"]
