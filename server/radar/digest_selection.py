"""Bounded daily evidence with a durable, source-based supplemental queue."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from pydantic import Field
from sqlalchemy import select

from .admission import visible_clause
from .config import RadarConfig
from .models import Article, Digest
from .schemas import Story

MAX_SUPPLEMENTAL_ITEMS = 5
DISPLAY_PREFIXES = ("补充｜", "含补充｜")
STORY_TITLE_LIMIT = Story.model_json_schema()["properties"]["title"]["maxLength"]
EVIDENCE_FIELDS = (
    "id", "title", "text", "url", "author", "published_at", "published_precision",
    "topics", "platform", "metrics",
)


@dataclass
class DigestSelection:
    articles: list[dict] = field(default_factory=list)
    supplemental_ids: set[str] = field(default_factory=set)


class StoredDigestStory(Story):
    """API display shape: model title (up to 160) plus a server badge (up to 4).

    This is deliberately separate from the unchanged provider Story contract;
    no model-generated title receives the additional display-only allowance.
    """

    title: str = Field(min_length=1, max_length=STORY_TITLE_LIMIT + max(map(len, DISPLAY_PREFIXES)))
    supplemental: bool
    supplemental_source_ids: list[str]


def select_digest_articles(session, config: RadarConfig, day: date,
                           start: datetime, end: datetime) -> DigestSelection:
    cutoff = end - timedelta(hours=config.lookback_hours)
    # Actual published citations are the ledger: merely supplying a source to
    # the model does not mean it was reported. Only earlier editions count:
    # this edition and later supplemental reports must not block a historical
    # edition's explicitly requested rebuild from its original evidence.
    reported_ids = {
        uid
        for stories in session.scalars(select(Digest.stories).where(Digest.date < day.isoformat()))
        for story in stories
        for uid in story.get("source_ids", [])
    }
    reported_urls = set(session.scalars(
        select(Article.canonical_url).where(Article.id.in_(reported_ids))
    )) if reported_ids else set()
    rows = session.scalars(select(Article).where(
        visible_clause(),
        Article.published_at >= min(start, cutoff).isoformat(),
        Article.published_at < end.isoformat(),
    )).all()
    # Current-window evidence wins over a supplemental alias of the same URL.
    current = sorted((row for row in rows if row.published_at >= start.isoformat()),
                     key=lambda row: (-row.score, row.id))
    supplemental = sorted((row for row in rows if cutoff.isoformat() <= row.published_at < start.isoformat()),
                          key=lambda row: (row.collected_at, row.score, row.id), reverse=True)
    seen = set(reported_urls)

    def unreported(candidates):
        eligible = []
        for row in candidates:
            if row.id in reported_ids or row.canonical_url in seen:
                continue
            seen.add(row.canonical_url)
            eligible.append(row)
        return eligible

    current, supplemental = unreported(current), unreported(supplemental)
    total = config.provider.max_items
    reserve = min(MAX_SUPPLEMENTAL_ITEMS, max(1, total // 5), len(supplemental)) if total >= 2 else 0
    chosen_current = current[:total - reserve]
    chosen_supplemental = supplemental[:min(MAX_SUPPLEMENTAL_ITEMS, total - len(chosen_current))]
    # If one side is short, use its capacity on the other. A one-item edition
    # always gives current evidence precedence; supplements wait for capacity.
    remaining = total - len(chosen_current) - len(chosen_supplemental)
    chosen_current += current[len(chosen_current):len(chosen_current) + remaining]
    supplemental_ids = {row.id for row in chosen_supplemental}
    return DigestSelection(
        articles=[dict(
            {name: getattr(row, name) for name in EVIDENCE_FIELDS},
            supplemental=row.id in supplemental_ids,
        ) for row in chosen_current + chosen_supplemental],
        supplemental_ids=supplemental_ids,
    )


def mark_supplemental_stories(stories: list[Story], selection: DigestSelection) -> list[dict]:
    """Compute labels from validated citations, never from model-supplied flags."""
    allowed = {article["id"] for article in selection.articles}
    result = []
    for story in stories:
        # Revalidate the model contract before applying the display allowance.
        story = Story.model_validate(story.model_dump())
        ids = set(story.source_ids)
        if not ids.issubset(allowed):
            raise ValueError("Model cited a source outside the supplied evidence")
        supplemental = [uid for uid in dict.fromkeys(story.source_ids) if uid in selection.supplemental_ids]
        title = story.title
        while title.startswith(DISPLAY_PREFIXES):
            title = title.split("｜", 1)[1]
        if supplemental:
            title = ("补充｜" if ids.issubset(selection.supplemental_ids) else "含补充｜") + title
        result.append(StoredDigestStory.model_validate(dict(
            story.model_dump(), title=title, supplemental=bool(supplemental),
            supplemental_source_ids=supplemental,
        )).model_dump())
    return result
