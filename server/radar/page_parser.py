"""Extract a single document; never downloads URLs or executes page code."""
import asyncio
import io
import json
import logging
import sys

from .links import normalize_link, text_references, useful_link

MAX_TEXT = 60_000


def parse_page(data: bytes, content_type: str, url: str) -> dict:
    links = []
    partial = False
    if "pdf" in content_type or data.startswith(b"%PDF-"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("encrypted document")
        chunks = []
        page_count = len(reader.pages)
        for page in reader.pages[:40]:
            chunks.append(page.extract_text() or "")
            if sum(map(len, chunks)) > MAX_TEXT:
                partial = True
                break
        text = "\n\n".join(chunks)
        title = str((reader.metadata or {}).get("/Title") or "")
        partial = partial or page_count > 40
        links = text_references(text)
    elif "html" in content_type:
        from trafilatura import bare_extraction

        doc = bare_extraction(data, url=url, include_comments=False, include_links=True,
                              with_metadata=True, prune_xpath="//nav|//header|//footer|//aside|//form")
        if not doc or doc.body is None:
            raise ValueError("no readable body")
        title = doc.title or ""
        text = doc.text or doc.raw_text or ""
        for node in doc.body.iter("ref"):
            link = normalize_link(node.get("target", ""), url)
            if link and useful_link(link):
                links.append({"url": link, "label": " ".join(node.itertext())[:300]})
    elif content_type.startswith("text/plain"):
        title, text = "", data.decode("utf-8", errors="replace")
        links = text_references(text)
    else:
        raise ValueError("unsupported document")
    if len(text.strip()) < 80:
        raise ValueError("no readable body")
    return {"title": title[:500], "text": text[:MAX_TEXT],
            "partial": partial or len(text) > MAX_TEXT, "links": links[:30]}


async def extract_page(data: bytes, content_type: str, url: str) -> dict:
    # PDFs and HTML can contain decompression/CPU bombs; isolate the parser from the API worker.
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "radar.page_parser", content_type, url,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), timeout=30)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode or len(stdout) > 1_000_000:
        raise ValueError("page extraction unavailable")
    return json.loads(stdout)


if __name__ == "__main__":
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    logging.disable(logging.CRITICAL)
    print(json.dumps(parse_page(sys.stdin.buffer.read(8_000_001), sys.argv[1], sys.argv[2]), ensure_ascii=False))
