"""Translate vendor wire formats into the existing X article contract.

Only direct quotes/replies are expanded. No follow-up profile/parent calls occur.
"""
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from .sources import SourceUnavailable, get_json
from .x_data_config import vendor_secret

TWITTERAPI_SEARCH = "https://api.twitterapi.io/twitter/tweet/advanced_search"


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing publication time")
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        date = parsedate_to_datetime(value)
    if not date or date.tzinfo is None:
        raise ValueError("Publication time must include a timezone")
    return date.astimezone(UTC).isoformat()


def vendor_page(tweets, *, next_cursor="", has_next_page=False):
    if not isinstance(tweets, list) or type(has_next_page) is not bool or not isinstance(next_cursor, str):
        raise ValueError("Invalid vendor pagination")
    if has_next_page and not next_cursor:
        raise ValueError("Unfinished page has no recovery cursor")
    users, posts, expansions = {}, [], {}

    def user(raw):
        if not isinstance(raw, dict):
            raise ValueError("Missing author")
        uid, handle = raw.get("id"), raw.get("userName") or raw.get("username")
        if not isinstance(uid, str) or not uid.isdigit() or not isinstance(handle, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            raise ValueError("Invalid author identity")
        if uid in users:
            if users[uid]["username"].casefold() != handle.casefold():
                raise ValueError("Conflicting author identity")
            return uid
        profile = {"id": uid, "username": handle, "name": raw.get("name") or handle,
                   "description": raw.get("description") or "",
                   "public_metrics": {"followers_count": raw.get("followers", 0)},
                   "protected": raw.get("protected", False)}
        users[uid] = profile
        return uid

    def post(raw, *, direct=True):
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"].isdigit():
            raise ValueError("Invalid tweet identity")
        note = raw.get("note_tweet") or raw.get("noteTweet") or {}
        content = note.get("text") or raw.get("fullText") or raw.get("full_text") or raw.get("text")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Missing tweet text")
        entities = raw.get("entities") or {}
        mentions = []
        for m in entities.get("user_mentions", []):
            handle = m.get("screen_name")
            span = m.get("indices", [])
            if len(span) == 2 and handle:
                mentions.append({"username": handle, "start": span[0], "end": span[1]})
        result = {"id": raw["id"], "text": content, "created_at": timestamp(raw.get("createdAt") or raw.get("created_at")),
                  "author_id": user(raw.get("author")), "entities": {"urls": entities.get("urls", []), "mentions": mentions},
                  "public_metrics": {name: raw.get(vendor, 0) for name, vendor in (
                      ("like_count", "likeCount"), ("reply_count", "replyCount"), ("retweet_count", "retweetCount"),
                      ("quote_count", "quoteCount"), ("impression_count", "viewCount"))}}
        if note.get("entities"):
            result["note_tweet"] = {"text": content, "entities": note["entities"]}
        refs = []
        if direct:
            quote = raw.get("quoted_tweet") or raw.get("quotedTweet")
            qid = quote.get("id") if isinstance(quote, dict) else raw.get("quoteId")
            if isinstance(qid, str) and qid.isdigit():
                refs.append({"type": "quoted", "id": qid})
                try:
                    expansions[qid] = post(quote, direct=False)
                except (ValueError, TypeError, AttributeError):
                    pass  # Keep explicit unavailable quote attribution.
            parent_id = raw.get("inReplyToId")
            if isinstance(parent_id, str) and parent_id.isdigit():
                refs.append({"type": "replied_to", "id": parent_id})
                result["in_reply_to_user_id"] = raw.get("inReplyToUserId")
        result["referenced_tweets"] = refs
        return result

    for raw in tweets:
        posts.append(post(raw))
    return {"data": posts, "includes": {"users": list(users.values()), "tweets": list(expansions.values())},
            "meta": {"next_token": next_cursor if has_next_page else "",
                     "newest_id": max((p["id"] for p in posts), key=int, default="")}}


class TwitterAPIProvider:
    name = "twitterapi_io"
    page_size = 20

    def __init__(self, config, ledger):
        self.config, self.ledger = config, ledger

    def require_key(self):
        key = vendor_secret(self.config, self.config.x_data.api_key_env)
        if not key:
            raise SourceUnavailable("auth_required", "需要 TwitterAPI.io API Key；请在服务端配置 TWITTERAPI_IO_KEY。")
        return key

    async def fetch(self, client, params, scope):
        key = self.require_key()
        # Use provider-specific Unix time syntax; X v2 operators are not portable.
        query = re.sub(r"\bis:retweet\b", "filter:retweets", params["query"])
        for field, operator in (("start_time", "since_time"), ("end_time", "until_time")):
            seconds = int(datetime.fromisoformat(params[field].replace("Z", "+00:00")).timestamp())
            query += f" {operator}:{seconds}"
        maximum = self.page_size * self.config.x_data.tweet_price_usd
        call_id = self.ledger.reserve(self.name, scope, maximum)
        try:
            body = await get_json(client, TWITTERAPI_SEARCH, params={"query": query, "queryType": "Latest",
                "cursor": params.get("next_token", "")}, headers={"X-API-Key": key})
            if body.get("status") in ("error", "failed") or not isinstance(body.get("tweets"), list):
                raise ValueError("Vendor did not return a tweet page")
            count = len(body["tweets"])
            self.ledger.finish(call_id, cost_usd=max(count, 1) * self.config.x_data.tweet_price_usd,
                               returned=count)
            if count > self.page_size:
                raise SourceUnavailable("budget_exhausted", "供应商响应超过单页计费预留，已停止请求并保留费用记录。")
            if "has_next_page" not in body:
                raise ValueError("Missing pagination completeness")
            page = vendor_page(body["tweets"], has_next_page=body["has_next_page"], next_cursor=body.get("next_cursor", ""))
            # The fixed window is our contract even if upstream ignores a filter.
            start = datetime.fromisoformat(params["start_time"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(params["end_time"].replace("Z", "+00:00"))
            for post in page["data"]:
                if not start <= datetime.fromisoformat(post["created_at"]) < end:
                    raise ValueError("Vendor returned a tweet outside the requested window")
                if scope.startswith("watch:"):
                    author = next(u for u in page["includes"]["users"] if u["id"] == post["author_id"])
                    if author["username"].lower() != scope.removeprefix("watch:"):
                        raise ValueError("Vendor returned a different watched author")
            page["_cost_call_id"] = call_id
            return page
        except BaseException:
            # A parsed response was already settled; do not replace that charge.
            with self.ledger.transaction() as session:
                from .models import XDataCall
                row = session.get(XDataCall, call_id)
                if row.status == "reserved":
                    row.status = "unknown"
            raise
