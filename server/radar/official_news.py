"""Bounded, unauthenticated reads of publisher announcement indexes.

Only publisher dates and excerpts become source evidence. No model-generated
text, sitemap modification dates, recursive crawl or login session is involved.
"""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from .schemas import IncomingArticle
from .sources import SourceUnavailable

NEWS_SOURCES = {
    "seed": ("ByteDance Seed 官方公告", "https://seed.bytedance.com/en/blog"),
    "deepseek": ("DeepSeek 官方公告", "https://www.deepseek.com/en/news/"),
    "kimi": ("Kimi 官方研究", "https://www.kimi.com/en/blog/"),
    "minimax": ("MiniMax 官方公告", "https://ir.minimax.io/news-events/new-releases"),
}
NEWS_IDS = {"official-" + key for key in NEWS_SOURCES}
DATE_PATTERN = re.compile(r"\b(?:[A-Z][a-z]+ \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2})\b")


@dataclass
class NewsResult:
    items: list[IncomingArticle]
    status: str
    message: str


def publisher_date(value: str) -> datetime:
    # Publishers expose dates, not times. UTC midnight is the storage anchor;
    # published_precision=date prevents presenting it as an exact publish time.
    for form in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, form).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError("Publisher date unavailable")


def _item(kind, url, title, excerpt, published_at):
    parsed = urlsplit(url)
    paths = {
        "seed": {"seed.bytedance.com": ("/en/blog/",)},
        "deepseek": {"www.deepseek.com": ("/en/news/",)},
        "kimi": {"www.kimi.com": ("/en/blog/",), "github.com": ("/MoonshotAI/", "/kvcache-ai/"),
                 "huggingface.co": ("/moonshotai/",), "moonshotai.github.io": ("/",)},
        "minimax": {"www.minimax.io": ("/news/", "/blog/")},
    }[kind]
    if (parsed.scheme != "https" or parsed.hostname not in paths or parsed.username
            or parsed.password or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
        raise ValueError("Unexpected announcement URL")
    if not parsed.path.startswith(paths[parsed.hostname]):
        raise ValueError("Unexpected announcement path")
    if not title.strip():
        raise ValueError("Missing announcement title")
    return IncomingArticle(
        platform="web", external_id=f"official:{kind}:" + hashlib.sha256(url.encode()).hexdigest(),
        url=url, title=title.strip()[:500], text=(excerpt.strip() or title.strip())[:30000],
        author=NEWS_SOURCES[kind][0], published_at=published_at, published_precision="date",
        source_id="official-" + kind,
    )


def parse_news(kind: str, html: str) -> NewsResult:
    soup = BeautifulSoup(html, "html.parser")
    items, skipped = {}, 0
    if kind == "seed":
        script = next((node.get_text().strip() for node in soup.select("script")
                       if node.get_text().strip().startswith("window._ROUTER_DATA = ")), "")
        if not script:
            raise ValueError("Seed announcement index missing")
        # Parse JSON, never evaluate JavaScript from a page.
        data = json.JSONDecoder().raw_decode(script.split(" = ", 1)[1])[0]
        page = data["loaderData"]["(locale$)/blog/page"]
        rows = page["article_list"]
        if not isinstance(rows, list) or not rows:
            raise ValueError("Seed announcement list missing")
        for row in rows[:100]:
            try:
                meta, content = row["ArticleMeta"], row["ArticleSubContentEn"]
                if meta.get("StatusEn") != 2:
                    continue
                slug = content["TitleKey"]
                if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9-]+", slug):
                    raise ValueError("Invalid announcement slug")
                value = meta["PublishDate"]  # UpdateTime and pinned order are not publication dates.
                if type(value) is not int or value <= 0:
                    raise ValueError("Missing publication date")
                item = _item(kind, "https://seed.bytedance.com/en/blog/" + slug,
                             content["Title"], content.get("Abstract", ""),
                             publisher_date(datetime.fromtimestamp(value / 1000, ZoneInfo("Asia/Shanghai"))
                                            .date().isoformat()))
                items[item.url] = item
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                skipped += 1
    else:
        if kind == "deepseek":
            cards = [a for a in soup.select('a[href^="/en/news/"]') if a.select_one("h2,h3")]
        elif kind == "kimi":
            cards = soup.select(".menu-card:has(.card-date)")
        elif kind == "minimax":
            cards = [a for a in soup.select("a[href]") if a.select_one("article time")]
        else:
            raise ValueError("Unsupported announcement source")
        for card in cards[:100]:
            try:
                link = card if card.name == "a" else card.select_one("a[href]")
                heading = card.select_one("h2,h3,h4")
                date_node = card.select_one("time,.card-date")
                date_match = DATE_PATTERN.search((date_node or card).get_text(" ", strip=True))
                if link is None or heading is None or date_match is None:
                    raise ValueError("Incomplete announcement card")
                excerpt = next((p.get_text(" ", strip=True) for p in reversed(card.select("p"))
                                if p != date_node and not DATE_PATTERN.search(p.get_text())), "")
                item = _item(kind, urljoin(NEWS_SOURCES[kind][1], link["href"]),
                             heading.get_text(" ", strip=True), excerpt, publisher_date(date_match[0]))
                items[item.url] = item
            except (KeyError, TypeError, ValueError, AttributeError):
                skipped += 1
    if not items:
        raise ValueError("No dated publisher announcements found")
    ordered = sorted(items.values(), key=lambda item: (item.published_at, item.url), reverse=True)
    message = f"读取公告首页 {len(ordered)} 条原始标题/摘要；保留原发布日期，全文由既有网页队列读取。"
    if skipped:
        message += f" {skipped} 条格式不完整，已跳过并等待下轮复查。"
    return NewsResult(ordered, "partial" if skipped else "healthy", message)


async def fetch_news(client: httpx.AsyncClient, kind: str) -> NewsResult:
    url = NEWS_SOURCES[kind][1]
    async with asyncio.timeout(30):
        # A redirect can indicate a login/challenge or a moved index. Do not
        # forward credentials or silently read a different site as the publisher.
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code in (401, 403):
                raise SourceUnavailable("access_restricted", "官方公告页暂时限制读取，下轮自动重试。")
            response.raise_for_status()
            if "html" not in response.headers.get("content-type", ""):
                raise SourceUnavailable("error", "官方公告页没有返回 HTML，下轮自动重试。")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 5_000_000:
                    raise SourceUnavailable("error", "官方公告首页超过 5 MB，已停止读取。")
        return parse_news(kind, body.decode("utf-8"))
