import asyncio
from urllib.parse import urljoin, urlsplit

import httpx
from trafilatura import extract

# Only publisher domains included in our official source catalogue can be fetched.
# Imported arbitrary URLs and social links never cause server-side URL fetching.
PUBLISHERS = {
    "openai.com",
    "www.openai.com",
    "blog.google",
    "deepmind.google",
    "huggingface.co",
    "anthropic.com",
    "www.anthropic.com",
}


def allowed(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in PUBLISHERS
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


async def enrich(articles: list[dict]) -> list[dict]:
    semaphore = asyncio.Semaphore(3)
    async with httpx.AsyncClient(
        timeout=15, headers={"User-Agent": "AIRadar/0.1 (publisher article reader)"}
    ) as client:

        async def one(article):
            result = dict(article, evidence_type="source_excerpt")
            if not allowed(article["url"]):
                return result
            async with semaphore:
                try:
                    url = article["url"]
                    for _ in range(4):
                        if not allowed(url):
                            return result
                        async with client.stream("GET", url) as response:
                            if response.is_redirect:
                                url = urljoin(url, response.headers.get("location", ""))
                                continue
                            response.raise_for_status()
                            if "html" not in response.headers.get("content-type", ""):
                                return result
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                data.extend(chunk)
                                if len(data) > 3_000_000:
                                    return result
                        body = await asyncio.to_thread(extract, bytes(data), include_comments=False)
                        if body and len(body) > len(article["text"]):
                            result.update(text=body[:12000], evidence_type="publisher_article")
                        break
                except (httpx.HTTPError, ValueError):
                    # Preserve the original evidence and its honest excerpt label.
                    pass
            return result

        return await asyncio.gather(*(one(article) for article in articles))
