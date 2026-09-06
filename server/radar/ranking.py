import hashlib
import math
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .schemas import IncomingArticle

TOPICS = {
    "模型": r"\b(llm|model|gpt|claude|gemini|deepseek|inference|reasoning|multimodal)\b|大模型|多模态|推理",
    "产品": r"\b(agent|codex|chatgpt|copilot|launch|release|app)\b|产品|发布|智能体",
    "技术": r"\b(ai|artificial intelligence|machine learning|training|benchmark|robot|robotics|paper)\b|人工智能|训练|机器人|论文",
    "开源": r"\b(open.source|hugging.?face|weights|github)\b|开源|权重",
    "观点": r"\b(agi|future|safety|alignment|prediction)\b|未来|观点|对齐",
    "产业": r"\b(nvidia|gpu|funding|datacenter|data center|compute)\b|融资|算力|芯片",
}


def canonicalize(url: str) -> str:
    p = urlsplit(url)
    host = p.netloc.lower().replace("www.", "", 1)
    if host in ("twitter.com", "mobile.twitter.com", "mobile.x.com"):
        host = "x.com"
    query = [
        (k, v)
        for k, v in parse_qsl(p.query)
        if not k.startswith("utm_") and k not in {"fbclid", "gclid", "ref", "s", "t"}
    ]
    return urlunsplit(("https", host, p.path.rstrip("/"), urlencode(sorted(query)), ""))


def article_id(article: IncomingArticle) -> str:
    return hashlib.sha256(f"{article.platform}:{article.external_id}".encode()).hexdigest()[:32]


def classify(article: IncomingArticle) -> list[str]:
    text = f"{article.title} {article.text}"
    return [topic for topic, pattern in TOPICS.items() if re.search(pattern, text, re.I)]


def engagement(metrics: dict) -> int:
    return (
        metrics.get("like_count", 0)
        + metrics.get("reaction_count", 0)
        + 2 * metrics.get("retweet_count", 0)
        + 2 * metrics.get("share_count", 0)
        + metrics.get("reply_count", 0)
        + metrics.get("comment_count", 0)
        + 2 * metrics.get("quote_count", 0)
    )


def rank(article: IncomingArticle, priority: bool, authority: float, now: datetime | None = None) -> float:
    age = max(0, ((now or datetime.now(UTC)) - article.published_at).total_seconds() / 3600)
    return round(
        (
            12 * math.log1p(engagement(article.metrics))
            + 28 * priority
            + 18 * authority
            + 5 * len(classify(article))
        )
        / (1 + age / 36),
        2,
    )
