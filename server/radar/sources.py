import calendar
import re
from datetime import UTC, datetime, timedelta
from html import unescape

import feedparser
import httpx
from bs4 import BeautifulSoup

from .config import FeedConfig, RadarConfig, secret
from .links import html_references
from .schemas import IncomingArticle


class SourceUnavailable(Exception):
    def __init__(self, status: str, message: str, *, partial_items: list[IncomingArticle] | None = None):
        self.status, self.message = status, message
        self.partial_items = partial_items or []
        super().__init__(message)


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict,
    headers: dict,
    allow_partial: bool = False,
):
    response = await client.get(url, params=params, headers=headers)
    if response.status_code in (401, 403):
        raise SourceUnavailable("auth_required", "授权无效或缺少读取权限，请检查平台凭证与应用权限。")
    if response.status_code in (402, 429):
        raise SourceUnavailable("rate_limited", "平台额度或频率受限，本轮停止请求，等待下次采集。")
    response.raise_for_status()
    body = response.json()
    partial_posts = allow_partial and isinstance(body.get("data"), list) and bool(body["data"])
    if body.get("error") or (body.get("errors") and not partial_posts):
        raise SourceUnavailable("error", "平台返回部分或完整请求错误，请检查查询及账号访问权限。")
    return body


def strip_markup(text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", text)).strip()


async def fetch_rss(client: httpx.AsyncClient, feed: FeedConfig) -> list[IncomingArticle]:
    # URLs are trusted deployment configuration, never supplied by a public API caller.
    async with client.stream("GET", feed.url, follow_redirects=True) as response:
        response.raise_for_status()
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > 5_000_000:
                raise SourceUnavailable("error", "订阅内容超过 5 MB 限制。")
            chunks.append(chunk)
    parsed = feedparser.parse(b"".join(chunks))
    if not parsed.entries and parsed.bozo:
        raise SourceUnavailable("error", "订阅源格式无法解析。")
    items = []
    for entry in parsed.entries[:100]:
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if not stamp or not entry.get("link"):
            continue  # Never relabel undated content as today's news.
        body = entry.get("summary") or " ".join(c.get("value", "") for c in entry.get("content", []))
        title = strip_markup(entry.get("title", ""))[:500]
        if not title:
            continue
        items.append(
            IncomingArticle(
                platform="rss",
                external_id=entry.get("id", entry.link)[:200],
                url=entry.link,
                title=title,
                text=(strip_markup(body) or title)[:30000],
                author=feed.name,
                published_at=datetime.fromtimestamp(calendar.timegm(stamp), UTC),
                source_id=feed.id,
                references=html_references(body, entry.link),
            )
        )
    return items


def x_full_text(post: dict) -> str:
    # X returns long-form text separately from the shortened standard text field.
    return ((post.get("note_tweet") or {}).get("text") or post.get("text") or "").strip()


def x_text_with_quotes(post: dict, referenced: dict, users: dict) -> str:
    parts = [x_full_text(post)]
    for reference in post.get("referenced_tweets", []):
        if reference.get("type") != "quoted":
            continue
        quoted = referenced.get(reference["id"])
        if not quoted or not x_full_text(quoted):
            parts.append("[引用帖不可用，未取得原文]")
            continue
        author = users.get(quoted.get("author_id"), {})
        identity = "@" + author["username"] if author.get("username") else "作者未返回"
        stamp = quoted.get("created_at") or "发布时间未返回"
        # Context is evidence under the original author's attribution, not a new
        # post by the main author. Keep its date separate from the main post date.
        parts.append(f"[引用帖：{identity}，{stamp}]\n{x_full_text(quoted)}")
    return "\n\n".join(part for part in parts if part)[:30000]


def x_references(post: dict, referenced: dict) -> list[dict]:
    posts = [post] + [referenced[r["id"]] for r in post.get("referenced_tweets", [])
                      if r.get("type") == "quoted" and r["id"] in referenced]
    refs = {}
    for item in posts:
        for entities in [item.get("entities", {}), (item.get("note_tweet") or {}).get("entities", {})]:
            for entry in entities.get("urls", []):
                url = entry.get("unwound_url") or entry.get("expanded_url") or entry.get("url")
                if url and len(url) <= 4000:
                    refs[url] = {"url": url, "label": (entry.get("title") or "")[:300],
                                 "short_url": (entry.get("url") or "")[:4000]}
    return list(refs.values())[:30]


async def fetch_x(
    client: httpx.AsyncClient, config: RadarConfig, handles: list[str]
) -> list[IncomingArticle]:
    token = secret("X_BEARER_TOKEN")
    if not token:
        raise SourceUnavailable(
            "auth_required", "需要 X Developer Bearer Token；也可通过已授权的采集脚本导入。"
        )
    queries = [config.x_query]
    # Separate priority accounts from broad discovery so quiet expert posts can be found.
    for i in range(0, len(handles), 12):
        queries.append("(" + " OR ".join(f"from:{h}" for h in handles[i : i + 12]) + ") -is:retweet")
    start = (datetime.now(UTC) - timedelta(hours=min(config.lookback_hours, 167))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    items = {}
    for query in queries:
        params = {
            "query": query,
            "max_results": config.x_page_size,
            "start_time": start,
            "tweet.fields": "created_at,public_metrics,author_id,note_tweet,referenced_tweets,entities",
            "expansions": "author_id,referenced_tweets.id,referenced_tweets.id.author_id",
            "user.fields": "name,username",
        }
        for _ in range(config.x_max_pages):
            try:
                body = await get_json(
                    client,
                    "https://api.x.com/2/tweets/search/recent",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    allow_partial=True,
                )
            except SourceUnavailable as exc:
                exc.partial_items = list(items.values())
                raise
            except httpx.HTTPError as exc:
                raise SourceUnavailable(
                    "error",
                    "X 请求中断，下轮重试；已取得的内容单独保留。",
                    partial_items=list(items.values()),
                ) from exc
            users = {u["id"]: u for u in body.get("includes", {}).get("users", [])}
            referenced = {p["id"]: p for p in body.get("includes", {}).get("tweets", [])}
            for post in body.get("data", []):
                text = x_text_with_quotes(post, referenced, users)
                if not text:
                    continue  # A video without readable text is not textual evidence.
                author = users.get(post["author_id"], {})
                handle = author.get("username", "i")
                items[post["id"]] = IncomingArticle(
                    platform="x",
                    source_id="x",
                    external_id=post["id"],
                    url=f"https://x.com/{handle}/status/{post['id']}",
                    title=(x_full_text(post) or text)[:180],
                    text=text,
                    author=author.get("name", handle),
                    handle=handle,
                    published_at=post["created_at"],
                    metrics=post.get("public_metrics", {}),
                    references=x_references(post, referenced),
                )
            next_token = body.get("meta", {}).get("next_token")
            if not next_token:
                break
            params["next_token"] = next_token
    return list(items.values())


async def fetch_facebook(client: httpx.AsyncClient, config: RadarConfig) -> list[IncomingArticle]:
    token = secret("FACEBOOK_ACCESS_TOKEN")
    if not token or not config.facebook_page_ids:
        raise SourceUnavailable(
            "auth_required", "需要 Meta 访问 Token 和已获读取权限的 Page ID；普通登录不等于 Graph API 权限。"
        )
    items = []
    since = int((datetime.now(UTC) - timedelta(hours=config.lookback_hours)).timestamp())
    for page in config.facebook_page_ids:
        if not re.fullmatch(r"[0-9]+", page):
            raise SourceUnavailable("error", "Facebook Page ID 必须是数字。")
        params = {
            "fields": "id,message,created_time,permalink_url,from,shares,reactions.limit(0).summary(true),comments.limit(0).summary(true)",
            "limit": 100,
            "since": since,
        }
        for _ in range(config.x_max_pages):
            body = await get_json(
                client,
                f"https://graph.facebook.com/{config.facebook_version}/{page}/posts",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            for post in body.get("data", []):
                if not post.get("message"):
                    continue
                items.append(
                    IncomingArticle(
                        platform="facebook",
                        source_id="facebook",
                        external_id=post["id"],
                        url=post.get("permalink_url", f"https://www.facebook.com/{post['id']}"),
                        title=post["message"][:180],
                        text=post["message"][:30000],
                        author=post.get("from", {}).get("name", page),
                        handle=page,
                        published_at=post["created_time"],
                        metrics={
                            "reaction_count": post.get("reactions", {})
                            .get("summary", {})
                            .get("total_count", 0),
                            "comment_count": post.get("comments", {})
                            .get("summary", {})
                            .get("total_count", 0),
                            "share_count": post.get("shares", {}).get("count", 0),
                        },
                    )
                )
            paging = body.get("paging", {})
            after = paging.get("cursors", {}).get("after")
            if not paging.get("next") or not after:
                break
            # Do not follow a remote pagination URL carrying credentials or arbitrary hosts.
            params["after"] = after
    return items


async def fetch_anthropic(client: httpx.AsyncClient) -> list[IncomingArticle]:
    """Read the public publisher page, using visible semantic markup, without login or undocumented APIs."""
    response = await client.get("https://www.anthropic.com/news", follow_redirects=True)
    response.raise_for_status()
    if len(response.content) > 5_000_000:
        raise SourceUnavailable("error", "Anthropic 页面超过大小限制。")
    soup = BeautifulSoup(response.text, "html.parser")
    items = {}
    for anchor in soup.select('a[href^="/news/"]'):
        stamp = anchor.find("time")
        title = anchor.select_one('h2, h3, h4, [class*="title"]')
        if not stamp or not title:
            continue
        try:
            published = datetime.strptime(stamp.get_text(strip=True), "%b %d, %Y").replace(tzinfo=UTC)
        except ValueError:
            continue
        link = "https://www.anthropic.com" + anchor["href"]
        paragraph = anchor.find("p")
        headline = title.get_text(" ", strip=True)
        body = paragraph.get_text(" ", strip=True) if paragraph else headline
        previous = items.get(link)
        if previous and len(previous.text) >= len(body):
            continue
        items[link] = IncomingArticle(
            platform="web",
            source_id="anthropic",
            external_id=link,
            url=link,
            title=headline[:500],
            text=body[:30000],
            author="Anthropic",
            published_at=published,
            published_precision="date",
        )
    if not items:
        raise SourceUnavailable("error", "Anthropic 页面结构变化，未解析出带日期的新闻。")
    return sorted(items.values(), key=lambda item: item.published_at, reverse=True)
