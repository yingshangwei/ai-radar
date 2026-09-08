"""Bounded public research discovery using original paper metadata and abstracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import feedparser
import httpx

from .links import normalize_link
from .schemas import IncomingArticle

if TYPE_CHECKING:
    from .config import ResearchConfig

HF_API = "https://huggingface.co/api/daily_papers"
ARXIV_API = "https://export.arxiv.org/api/query"
ABSTRACT_PREFIX = "arXiv preprint · Author abstract\n\n"
MAX_RESPONSE_BYTES = 5_000_000
PAPER_ID = re.compile(r"(?P<base>(?:\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}|[a-z][a-z.-]*/\d{7}))"
                      r"(?:v(?P<version>[1-9]\d*))?", re.I)
THEORY = re.compile(
    r"\b(?:sample[\s-]+complexity|scaling[\s-]+laws?|"
    r"information[\s-]+theor(?:y|etic)|optimization[\s-]+bounds?|"
    r"regret[\s-]+bounds?|PAC[\s-]+(?:Bayes(?:ian)?|learn(?:ing|ability))|"
    r"statistical[\s-]+learning[\s-]+theory)\b", re.I,
)
GENERAL_THEORY = re.compile(r"\b(?:generali[sz]ation|convergence)\b", re.I)
THEORY_EVIDENCE = re.compile(r"\b(?:theoretical|theorems?|proofs?|prove[sd]?|proving|bounds?|guarantees?)\b", re.I)


@dataclass
class ResearchResult:
    items: list[IncomingArticle]
    status: str
    message: str


class _FetchError(Exception):
    def __init__(self, status: str, message: str):
        self.status, self.message = status, message
        super().__init__(message)


async def _download(client: httpx.AsyncClient, url: str, params: dict) -> bytes:
    try:
        async with client.stream("GET", url, params=params, follow_redirects=True) as response:
            if response.status_code == 429:
                raise _FetchError("rate_limited", "公开论文接口访问频率受限，等待下次采集。")
            if response.status_code in (401, 403):
                raise _FetchError("access_restricted", "公开论文接口暂时拒绝访问；不代表需要账号授权。")
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise _FetchError("error", "公开论文响应超过 5 MB 限制。")
                chunks.append(chunk)
        return b"".join(chunks)
    except httpx.HTTPError:
        raise _FetchError("error", "公开论文请求未完成，等待下次采集。") from None


def _paper_id(value: str) -> tuple[str, int | None]:
    if not isinstance(value, str):
        raise ValueError("paper_id")
    value = value.strip()
    if value.startswith(("https://", "http://")):
        parsed = urlsplit(value)
        if (parsed.hostname not in {"arxiv.org", "export.arxiv.org"} or parsed.username
                or parsed.password or parsed.port or parsed.query or parsed.fragment
                or not parsed.path.startswith("/abs/")):
            raise ValueError("paper_id")
        value = parsed.path.removeprefix("/abs/")
    match = PAPER_ID.fullmatch(value)
    if not match:
        raise ValueError("paper_id")
    return match["base"], int(match["version"]) if match["version"] else None


def _text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing_text")
    return value.strip()


def _published(value) -> datetime:
    value = _text(value)
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("undated_paper")
    return stamp.astimezone(UTC)


def _authors(value) -> str:
    if not isinstance(value, list) or not value:
        raise ValueError("missing_authors")
    names = [_text(author.get("name")) for author in value if isinstance(author, dict)]
    if len(names) != len(value):
        raise ValueError("invalid_authors")
    return ", ".join(names)[:200]


def _count(value) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid_metric")
    return value


def _article(*, identifier, title, summary, authors, published, source, metrics,
             references=None, published_precision="timestamp"):
    base, version = _paper_id(identifier)
    text = ABSTRACT_PREFIX + _text(summary)
    if len(text) > 30000:
        raise ValueError("abstract_too_long")  # Do not silently publish a truncated author abstract.
    if version is not None:
        metrics = {**metrics, "arxiv_version": version}
    return IncomingArticle(
        platform="web", external_id="arxiv:" + base, url="https://arxiv.org/abs/" + base,
        title=_text(title)[:500], text=text, author=_authors(authors), published_at=published,
        source_id=source, metrics=metrics, references=references or [], published_precision=published_precision,
    )


def _hf_article(row, observed_at: int) -> IncomingArticle:
    if not isinstance(row, dict) or not isinstance(row.get("paper"), dict):
        raise ValueError("invalid_paper")
    paper = row["paper"]
    base, _ = _paper_id(paper.get("id"))
    votes = _count(paper.get("upvotes"))
    comments = _count(row.get("numComments"))
    references = [{"url": "https://huggingface.co/papers/" + base, "label": "Hugging Face 论文讨论"}]
    for field, label in (("projectPage", "论文项目页"), ("githubRepo", "论文代码仓库")):
        value = paper.get(field)
        if isinstance(value, str) and (url := normalize_link(value)):
            references.append({"url": url, "label": label})
    return _article(
        identifier=paper.get("id"), title=paper.get("title"), summary=paper.get("summary"),
        authors=paper.get("authors"), published=_published(paper.get("publishedAt")),
        source="hf-papers", published_precision="date",
        references=list({r["url"]: r for r in references}.values()),
        metrics={"like_count": votes, "comment_count": comments, "hf_upvotes": votes,
                 "hf_comments": comments, "hf_votes_observed_at": observed_at},
    )


def _merge_hf(items: dict[str, IncomingArticle], item: IncomingArticle):
    previous = items.get(item.external_id)
    if previous is None:
        items[item.external_id] = item
        return
    # Version choice and genuine observed counters are separate; never add votes
    # from two views of the same paper or let an older version replace a newer one.
    selected = item if item.metrics.get("arxiv_version", 0) >= previous.metrics.get("arxiv_version", 0) else previous
    metrics = dict(selected.metrics)
    for key in ("like_count", "comment_count", "hf_upvotes", "hf_comments"):
        metrics[key] = max(previous.metrics[key], item.metrics[key])
    references = {r.url: r for paper in (previous, item) for r in paper.references}
    items[item.external_id] = selected.model_copy(update={"metrics": metrics, "references": list(references.values())})


async def fetch_hf_papers(
    client: httpx.AsyncClient, config: ResearchConfig, lookback_hours: int,
) -> ResearchResult:
    if not config.hf_enabled:
        return ResearchResult([], "disabled", "Hugging Face 论文来源未启用。")
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=lookback_hours)
    candidates, invalid, notes, items = 0, 0, [], {}
    for sort in ("trending", "publishedAt"):
        try:
            payload = await _download(client, HF_API, {"sort": sort, "limit": 100, "p": 0})
            try:
                rows = json.loads(payload)
            except (ValueError, UnicodeError):
                raise _FetchError("error", "论文接口返回的 JSON 无法解析。") from None
            if not isinstance(rows, list):
                raise _FetchError("error", "论文接口未返回论文列表。")
        except _FetchError as exc:
            if sort == "trending":
                return ResearchResult([], exc.status, exc.message)
            notes.append(exc.message)
            break
        candidates += min(len(rows), 100)
        invalid += int(len(rows) > 100)
        for row in rows[:100]:
            if isinstance(row, dict) and isinstance(row.get("paper"), dict) and row["paper"].get("withdrawnAt"):
                continue
            try:
                item = _hf_article(row, int(datetime.now(UTC).timestamp()))
            except (ValueError, TypeError, KeyError, OverflowError):
                invalid += 1
                continue
            if cutoff <= item.published_at <= now and item.metrics["hf_upvotes"] >= config.hf_min_upvotes:
                _merge_hf(items, item)
        if len(items) >= config.hf_limit:
            break
    selected = sorted(items.values(), key=lambda item: (
        -item.metrics["hf_upvotes"], -item.metrics["hf_comments"],
        -item.published_at.timestamp(), item.external_id,
    ))[:config.hf_limit]
    message = f"读取 {candidates} 条社区候选，按论文原日期和真实投票入选 {len(selected)} 篇；最多读取两页列表。"
    if invalid:
        message += f" {invalid} 条候选无法完整验证，已跳过或限制范围。"
    if notes:
        message += " " + " ".join(notes)
    return ResearchResult(selected, "partial" if invalid or notes else "healthy", message)


async def fetch_arxiv_theory(
    client: httpx.AsyncClient, config: ResearchConfig, lookback_hours: int,
) -> ResearchResult:
    if not config.arxiv_enabled:
        return ResearchResult([], "disabled", "arXiv 理论论文来源未启用。")
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=lookback_hours)
    try:
        payload = await _download(client, ARXIV_API, {
            "search_query": "(cat:cs.LG OR cat:stat.ML)", "sortBy": "submittedDate",
            "sortOrder": "descending", "start": 0, "max_results": config.arxiv_candidates,
        })
    except _FetchError as exc:
        return ResearchResult([], exc.status, exc.message)
    feed = feedparser.parse(payload)
    if (not feed.entries and feed.bozo) or feed.get("feed", {}).get("title") == "Error":
        return ResearchResult([], "error", "arXiv 返回的论文订阅格式无法解析。")
    if not feed.get("version", "").startswith("atom"):
        return ResearchResult([], "error", "arXiv 未返回预期的 Atom 论文列表。")
    candidates = min(len(feed.entries), config.arxiv_candidates)
    invalid = int(bool(feed.bozo)) + int(len(feed.entries) > config.arxiv_candidates)
    items = {}
    for entry in feed.entries[:config.arxiv_candidates]:
        try:
            stamp = _published(entry.get("published"))
            title, summary = _text(entry.get("title")), _text(entry.get("summary"))
            item = _article(
                identifier=entry.get("id"), title=title, summary=summary, authors=entry.get("authors"),
                published=stamp, source="arxiv-theory", metrics={},
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            invalid += 1
            continue
        evidence = title + "\n" + summary
        theoretical = THEORY.search(evidence) or (GENERAL_THEORY.search(evidence) and THEORY_EVIDENCE.search(evidence))
        if not cutoff <= stamp <= now or not theoretical:
            continue
        previous = items.get(item.external_id)
        if previous is None or item.metrics.get("arxiv_version", 0) > previous.metrics.get("arxiv_version", 0):
            items[item.external_id] = item
    selected = sorted(items.values(), key=lambda item: (-item.published_at.timestamp(), item.external_id))[:config.arxiv_limit]
    message = (f"读取 {candidates} 条 arXiv 候选，按原论文日期和理论主题入选 {len(selected)} 篇；"
               "这是作者摘要与主题筛选，不代表社区热度或同行评审。")
    if invalid:
        message += f" {invalid} 条候选无法完整验证，已跳过或限制范围。"
    return ResearchResult(selected, "partial" if invalid else "healthy", message)
