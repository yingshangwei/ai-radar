"""Crawl4AI extraction of an already rendered, access-checked HTML document.

Navigation belongs to browser_worker and its guarded Playwright context. This
module never gives a remote URL to AsyncWebCrawler: the URL here is only the base
for source links. Official scraping and Markdown APIs run in a bounded child
process with network and process creation denied, without LLM extraction.
"""
import asyncio
import contextlib
import importlib.metadata
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from .links import normalize_link, useful_link
from .page_parser import MAX_TEXT, extract_page

CRAWL4AI_VERSION = "0.9.3"
MAX_HTML = 8_000_000
MAX_OUTPUT = 1_000_000
PARSER_TIMEOUT = 20
FALLBACK_TIMEOUT = 10


class CrawlExtractionError(ValueError):
    def __init__(self, code="extraction_failed"):
        self.code = code
        super().__init__(code)


def _validate_input(data, url):
    if not isinstance(data, bytes) or len(data) > MAX_HTML:
        raise ValueError("HTML exceeds extraction limit")
    normalized = normalize_link(url)
    if not normalized or data.lstrip().startswith(b"%PDF-"):
        raise ValueError("only captured HTTP(S) HTML is supported")
    return normalized


def _deny_network_and_processes(event, _args):
    # Defense in depth for optional upstream features or import-time telemetry.
    # This hook is installed only in the child, never in the browser worker.
    if event.startswith("socket.") or event in {
        "subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.fork",
    }:
        raise PermissionError("captured HTML extraction cannot access the network or launch processes")


def _reading_region(tree):
    """Honor publisher body boundaries without guessing from article wording.

    Keep an outer article when articles are nested: footnotes and corrections can
    be outside the inner article, without explicit fragment links back to it.
    Ambiguous multi-article pages retain their complete main region.
    """
    selectors = [
        '//*[contains(concat(" ", normalize-space(@class), " "), " blog-content ")]',
        '//article[not(ancestor::article)]',
        '//main | //*[@role="main"]',
    ]
    for selector in selectors:
        regions = [node for node in tree.xpath(selector) if len(node.text_content().strip()) >= 80]
        if len(regions) == 1:
            return regions[0]
    for node in tree.xpath("//body/header"):
        node.drop_tree()
    return tree


def parse_crawl_page(data: bytes, url: str) -> dict:
    """Pure captured-HTML extraction; caller supplies process/resource isolation."""
    url = _validate_input(data, url)
    try:
        version = importlib.metadata.version("crawl4ai")
    except importlib.metadata.PackageNotFoundError:
        raise CrawlExtractionError("dependency_missing") from None
    if version != CRAWL4AI_VERSION:
        raise CrawlExtractionError("dependency_version_mismatch")

    from crawl4ai import DefaultMarkdownGenerator, LXMLWebScrapingStrategy
    from lxml import html

    from .math_text import protect_html_math, restore_html_math, truncate_math

    source_html, formulas = protect_html_math(data)
    source_tree = html.fromstring(source_html, parser=html.HTMLParser(no_network=True))
    bases = source_tree.xpath("//head/base[@href]")
    # HTML base changes relative references, never this document's identity.
    # Unsupported bases discard relative links instead of inventing a new target.
    link_base = (normalize_link(bases[0].get("href", ""), url) or "") if bases else url
    del source_tree, bases
    scraped = LXMLWebScrapingStrategy().scrap(
        url, source_html,
        excluded_tags=["nav", "footer", "aside", "form", "script", "style", "noscript",
                       "iframe", "object", "embed", "base", "button", "input", "select", "textarea",
                       "template", "dialog", "devsite-feedback", "devsite-content-footer", "devsite-actions"],
        excluded_selector=('[hidden], [aria-hidden="true"], [role="navigation"], '
                           '[contenteditable]:not([contenteditable="false"]), '
                           '[data-target="DiscussionEvents"], [data-target="BlogThumbnail"], '
                           '[data-target="UpvoteControl"]'),
        remove_forms=True, remove_comments=True, exclude_all_images=True,
        link_preview_config=None, score_links=False,
    )
    if not scraped.success or not scraped.cleaned_html:
        raise CrawlExtractionError("no_readable_body")
    tree = _reading_region(html.fromstring(scraped.cleaned_html))
    visible_text = tree.text_content().strip()
    if len(visible_text) < 80:
        raise CrawlExtractionError("no_readable_body")

    links, seen = [], set()
    for node in tree.xpath(".//a[@href]"):
        link = normalize_link(node.get("href", ""), link_base)
        if link and link not in seen and useful_link(link):
            links.append({"url": link, "label": " ".join(node.itertext()).strip()[:300]})
            seen.add(link)
        if len(links) == 30:
            break
    markdown = DefaultMarkdownGenerator(options={
        "body_width": 0, "ignore_images": True, "ignore_links": True,
        "single_line_break": False, "mark_code": True,
    }).generate_markdown(html.tostring(tree, encoding="unicode"), base_url=url, citations=False)
    text = restore_html_math(markdown.raw_markdown.strip(), formulas)
    if len(text) < 80 or text.startswith("Error converting HTML to markdown:"):
        raise CrawlExtractionError("no_readable_body")
    title = str((scraped.metadata or {}).get("title") or "")[:500]
    if not title:
        headings = tree.xpath(".//h1")
        title = headings[0].text_content().strip()[:500] if headings else ""
    title = restore_html_math(title, formulas)
    return {"title": truncate_math(title, 500), "text": truncate_math(text, MAX_TEXT), "partial": len(text) > MAX_TEXT,
            "links": links, "extraction_engine": "crawl4ai", "extraction_version": CRAWL4AI_VERSION}


async def _extract_isolated(data: bytes, url: str) -> dict:
    # The parser needs HTML, not the browser profile or worker credentials.
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
           if key in os.environ}
    env.update({"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                "LITELLM_MODE": "PRODUCTION", "PYTHON_DOTENV_DISABLED": "1", "DO_NOT_TRACK": "1",
                "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})
    with tempfile.TemporaryDirectory(prefix="radar-crawl-parser-") as temporary:
        env.update({"HOME": temporary, "CRAWL4_AI_BASE_DIRECTORY": temporary})
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "radar.crawl_reader", url,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env,
            cwd=temporary,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(data), PARSER_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
    if len(stdout) > MAX_OUTPUT:
        raise CrawlExtractionError("output_too_large")
    try:
        result = json.loads(stdout)
    except (ValueError, UnicodeError):
        raise CrawlExtractionError("invalid_parser_output") from None
    if process.returncode:
        code = result.get("error") if isinstance(result, dict) else None
        if code not in {"dependency_missing", "dependency_version_mismatch", "no_readable_body"}:
            code = "extraction_failed"
        raise CrawlExtractionError(code)
    if (not isinstance(result, dict) or result.get("extraction_engine") != "crawl4ai"
            or not isinstance(result.get("text"), str) or not 80 <= len(result["text"]) <= MAX_TEXT
            or not isinstance(result.get("links"), list) or len(result["links"]) > 30):
        raise CrawlExtractionError("invalid_parser_output")
    return result


async def extract_crawl_page(data: bytes, url: str) -> dict:
    """Return the existing document contract, with observable extractor provenance.

    A dependency or extraction failure falls back to the existing isolated HTML
    parser. Cancellation propagates so abandoned jobs do not start more work.
    """
    url = _validate_input(data, url)
    try:
        return await _extract_isolated(data, url)
    except (CrawlExtractionError, TimeoutError, OSError) as exc:
        reason = exc.code if isinstance(exc, CrawlExtractionError) else (
            "extraction_timeout" if isinstance(exc, TimeoutError) else "parser_unavailable")
        # Keep both engines inside one browser request budget. extract_page kills
        # and reaps its own parser when this deadline cancels it.
        result = await asyncio.wait_for(extract_page(data, "text/html", url), FALLBACK_TIMEOUT)
        return {**result, "extraction_engine": "trafilatura_fallback", "extraction_fallback_reason": reason}


def _main():
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    logging.disable(logging.CRITICAL)
    sys.addaudithook(_deny_network_and_processes)
    try:
        result = parse_crawl_page(sys.stdin.buffer.read(MAX_HTML + 1), sys.argv[1])
    except Exception as exc:
        code = exc.code if isinstance(exc, CrawlExtractionError) else "extraction_failed"
        print(json.dumps({"error": code}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
