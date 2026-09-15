import calendar
import re
from datetime import UTC, datetime, timedelta
from html import unescape

import feedparser
import httpx
from bs4 import BeautifulSoup

from .config import FeedConfig, RadarConfig, secret
from .links import html_references
from .schemas import AssociatedEntity, IncomingArticle

X_TWEET_FIELDS = "created_at,public_metrics,author_id,note_tweet,referenced_tweets,entities,in_reply_to_user_id"
X_EXPANSIONS = ("author_id,referenced_tweets.id,referenced_tweets.id.author_id,"
                "entities.mentions.username,in_reply_to_user_id")
X_USER_FIELDS = "id,name,username,description,public_metrics,url,protected"
X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


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
    if response.status_code == 402:
        # Do not echo provider payloads: they can contain account IDs or tokens.
        raise SourceUnavailable("payment_required", "API 余额不足或计费额度受限（HTTP 402）。"
                                "请在平台开发者控制台检查余额及消费上限；处理后下轮自动恢复，也可点击拉取最新。")
    if response.status_code == 429:
        raise SourceUnavailable("rate_limited", "平台请求频率受限（HTTP 429），本轮停止请求，冷却后下轮自动重试。")
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


def x_references(post: dict, referenced: dict, users: dict | None = None) -> list[dict]:
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
    replies = []
    for relation in post.get("referenced_tweets", []):
        if relation.get("type") != "replied_to":
            continue
        uid = relation.get("id", "")
        if not isinstance(uid, str) or not uid.isdigit():
            continue
        parent = referenced.get(uid, {})
        author = (users or {}).get(parent.get("author_id"), {})
        replies.append({"kind": "reply", "url": f"https://x.com/i/status/{uid}",
                        "author": author.get("name", "")[:200], "handle": author.get("username", "")[:200],
                        "published_at": parent.get("created_at")})
        break  # X replies identify one direct parent, never a deeper conversation.
    return replies + list(refs.values())[:30 - len(replies)]


def _mention_text(text, mention) -> str | None:
    """Preserve only the literal mention span, never infer a surrounding quote."""
    start, end, username = mention.get("start"), mention.get("end"), mention.get("username")
    if (not isinstance(text, str) or not isinstance(username, str)
            or type(start) is not int or type(end) is not int or start < 0 or end <= start):
        return None
    choices = [text[start:end]]
    try:
        choices.append(text.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le"))
    except UnicodeError:
        pass
    return next((value for value in choices if value.casefold() == ("@" + username).casefold()), None)


def extract_x_entities(post: dict, referenced: dict, users: list[dict]) -> list[AssociatedEntity]:
    """Resolve direct relations against supplied real profiles without extra API calls."""
    profiles, ambiguous_ids, handles = {}, set(), {}
    for user in users:
        if not isinstance(user, dict):
            continue
        uid, username = user.get("id"), user.get("username")
        if not isinstance(uid, str) or not uid.isascii() or not uid.isdigit():
            continue
        if not isinstance(username, str) or not X_HANDLE.fullmatch(username):
            continue
        if uid in profiles and profiles[uid] != user:
            ambiguous_ids.add(uid)
        profiles[uid] = user
        handles.setdefault(username.casefold(), set()).add(uid)
    selected = {}

    def add(uid, relation, *, username=None, matched_text=None):
        if not isinstance(uid, str) or uid == post.get("author_id") or uid in selected or uid in ambiguous_ids:
            return
        profile = profiles.get(uid)
        if profile is None or len(handles[profile["username"].casefold()]) != 1:
            return
        if username is not None and profile["username"].casefold() != username.casefold():
            return
        metrics = profile.get("public_metrics")
        if not isinstance(metrics, dict):
            return
        name, description = profile.get("name"), profile.get("description", "")
        if not isinstance(name, str) or not name.strip() or not isinstance(description, str):
            return
        try:
            entity = AssociatedEntity(
                external_id=uid, handle=profile["username"], name=name[:120], description=description[:1000],
                followers_count=metrics.get("followers_count"), url="https://x.com/" + profile["username"],
                relation=relation, matched_text=matched_text,
            )
        except ValueError:
            return
        if len(selected) < 30:
            selected[uid] = entity

    # note_tweet entities belong to the same main post, not to an expanded quote.
    for content in (post, post.get("note_tweet")):
        if not isinstance(content, dict) or not isinstance(content.get("entities"), dict):
            continue
        mentions = content["entities"].get("mentions", [])
        if not isinstance(mentions, list):
            continue
        for mention in mentions:
            if not isinstance(mention, dict) or not isinstance(mention.get("username"), str):
                continue
            if not X_HANDLE.fullmatch(mention["username"]):
                continue
            add(mention.get("id"), "mention", username=mention["username"],
                matched_text=_mention_text(content.get("text"), mention))

    reply_author = post.get("in_reply_to_user_id")
    references = post.get("referenced_tweets", [])
    for relation in references if isinstance(references, list) else []:
        if not isinstance(relation, dict) or relation.get("type") not in {"quoted", "replied_to"}:
            continue
        uid = relation.get("id")
        parent = referenced.get(uid, {}) if isinstance(uid, str) else {}
        if not isinstance(parent, dict):
            continue
        author_id = parent.get("author_id")
        if relation["type"] == "quoted":
            add(author_id, "quote")
        elif reply_author is None:
            reply_author = author_id
        elif author_id is not None and author_id != reply_author:
            reply_author = ""
    if reply_author:
        add(reply_author, "reply")
    return list(selected.values())


def x_page_items(body: dict) -> list[IncomingArticle]:
    if not isinstance(body.get("data", []), list):
        raise ValueError("Invalid X page structure")
    includes = body.get("includes") if isinstance(body.get("includes"), dict) else {}
    raw_users = includes.get("users") if isinstance(includes.get("users"), list) else []
    raw_tweets = includes.get("tweets") if isinstance(includes.get("tweets"), list) else []
    users = {user["id"]: user for user in raw_users
             if isinstance(user, dict) and isinstance(user.get("id"), str)}
    referenced = {post["id"]: post for post in raw_tweets
                  if isinstance(post, dict) and isinstance(post.get("id"), str)}
    items = {}
    for post in body.get("data", []):
        content = x_text_with_quotes(post, referenced, users)
        if not content:
            continue
        author = users.get(post["author_id"], {})
        handle = author.get("username", "i")
        items[post["id"]] = IncomingArticle(
            platform="x", source_id="x", external_id=post["id"],
            url=f"https://x.com/{handle}/status/{post['id']}",
            title=(x_full_text(post) or content)[:180], text=content,
            author=author.get("name", handle), handle=handle,
            published_at=post["created_at"], metrics=post.get("public_metrics", {}),
            references=x_references(post, referenced, users),
            entities=extract_x_entities(post, referenced, raw_users),
            author_external_id=post["author_id"] if isinstance(post.get("author_id"), str)
            and len(post["author_id"]) <= 100 else "",
        )
    return list(items.values())


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
            "tweet.fields": X_TWEET_FIELDS,
            "expansions": X_EXPANSIONS,
            "user.fields": X_USER_FIELDS,
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
            items.update({item.external_id: item for item in x_page_items(body)})
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
            try:
                body = await get_json(
                    client,
                    f"https://graph.facebook.com/{config.facebook_version}/{page}/posts",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except SourceUnavailable as exc:
                exc.partial_items = items
                raise
            except httpx.HTTPError as exc:
                raise SourceUnavailable(
                    "error",
                    "Facebook 请求中断，下轮重试；已取得的内容单独保留。",
                    partial_items=items,
                ) from exc
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
