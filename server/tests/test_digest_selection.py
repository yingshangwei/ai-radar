from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from radar import pipeline as pipeline_module
from radar.config import RadarConfig
from radar.daily_schedule import DailySchedule
from radar.db import database
from radar.digest_selection import (
    MAX_SUPPLEMENTAL_ITEMS,
    DigestSelection,
    StoredDigestStory,
    mark_supplemental_stories,
    select_digest_articles,
)
from radar.models import Article, Digest, SourceState
from radar.pipeline import Pipeline, as_dict, digest_window
from radar.schemas import DigestOutput, Story

DAY = date(2020, 9, 8)


@pytest.fixture
def store(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/supplemental.db")
    yield sessions
    engine.dispose()


def config(**overrides):
    return RadarConfig(**{
        "reading": {"enabled": False}, "translation": {"enabled": False},
        # These tests exercise selection with a deterministic generation stub;
        # actual review/publication is covered in test_summary_publication.py.
        "summary_review": {"enabled": False},
        "enrich_official_articles": True, **overrides,
    })


def article(session, uid, published, *, collected=None, score=50, url=None):
    url = url or f"https://example.org/{uid}"
    row = Article(
        id=uid, platform="x", source_id="fixture", external_id=uid,
        url=url, canonical_url=url, title="Synthetic AI model research",
        text="A synthetic language model result for this test.", author="Fixture",
        published_at=published.isoformat(), collected_at=(collected or published).isoformat(),
        metrics={"like_count": 50}, topics=["模型"], score=score,
    )
    session.add(row)
    session.flush()
    return row


def story(ids, title="Synthetic story"):
    return Story(title=title, summary="Synthetic summary.", why_it_matters="Synthetic test.",
                 category="模型", source_ids=ids)


def published(session, day, cfg, ids, *, count=None):
    start, end = digest_window(day, cfg)
    row = Digest(date=day.isoformat(), title="Fixture edition", overview="Fixture overview",
                 stories=[story(ids).model_dump()] if ids else [], provider="fixture",
                 source_count=len(ids) if count is None else count,
                 window_start=start.isoformat(), window_end=end.isoformat(), coverage=[])
    session.add(row)
    session.flush()
    return row


def selection(session, cfg, day=DAY):
    start, end = digest_window(day, cfg)
    return select_digest_articles(session, cfg, day, start, end)


def provider_spy(monkeypatch, groups=None):
    calls = []

    async def generate(rows, day):
        calls.append(deepcopy(rows))
        ids = [row["id"] for row in rows]
        return DigestOutput(title="Synthetic edition", overview="Synthetic overview.",
                            stories=[story(group) for group in (groups(rows) if groups else [[uid] for uid in ids])])

    monkeypatch.setattr(pipeline_module, "make_provider", lambda _: SimpleNamespace(generate=generate))
    return calls


@pytest.mark.asyncio
async def test_late_source_reaches_next_daily_once_without_rewriting_published_edition(store, monkeypatch):
    cfg = config()
    _, first_end = digest_window(DAY, cfg)
    pipeline = Pipeline(store, cfg)
    calls = provider_spy(monkeypatch)
    with store.begin() as session:
        article(session, "first", first_end - timedelta(hours=2))
    await pipeline.digest(DAY)
    with store.begin() as session:
        original_edition = deepcopy(as_dict(session.get(Digest, DAY.isoformat())))
        late = article(session, "late", first_end - timedelta(hours=1), collected=first_end + timedelta(hours=2))
        original_source = deepcopy(as_dict(late))
        article(session, "next", first_end + timedelta(hours=3))
    assert await DailySchedule(pipeline, clock=lambda: first_end + timedelta(hours=2)).run() is None
    await pipeline.digest(DAY)
    assert len(calls) == 1
    await pipeline.digest(DAY + timedelta(days=1))
    assert [row["id"] for row in calls[-1]] == ["next", "late"]
    assert calls[-1][-1]["published_at"] == original_source["published_at"]
    assert calls[-1][-1]["supplemental"] is True
    with store() as session:
        assert as_dict(session.get(Digest, DAY.isoformat())) == original_edition
        assert as_dict(session.get(Article, "late")) == original_source
        later = session.get(Digest, (DAY + timedelta(days=1)).isoformat())
        assert later.stories[1]["title"] == "补充｜Synthetic story"
        assert later.stories[1]["supplemental_source_ids"] == ["late"]
        assert not later.stories[0]["supplemental"]
        # Published citations remain the ledger after a process restart.
    restarted = Pipeline(store, cfg)
    with store.begin() as session:
        article(session, "third", first_end + timedelta(days=1, hours=3))
    await restarted.digest(DAY + timedelta(days=2))
    assert [row["id"] for row in calls[-1]] == ["third"]


@pytest.mark.asyncio
async def test_published_no_updates_stays_unchanged_and_late_content_goes_to_next_day(store, monkeypatch):
    cfg = config()
    _, end = digest_window(DAY, cfg)
    pipeline = Pipeline(store, cfg)
    with store.begin() as session:
        session.get(SourceState, "x").status = "healthy"
    await pipeline.digest(DAY)
    with store.begin() as session:
        before = deepcopy(as_dict(session.get(Digest, DAY.isoformat())))
        article(session, "late", end - timedelta(hours=1), collected=end + timedelta(hours=2))
    calls = provider_spy(monkeypatch)
    assert await DailySchedule(pipeline, clock=lambda: end + timedelta(hours=2)).run() is None
    await pipeline.digest(DAY)
    assert calls == []
    await pipeline.digest(DAY + timedelta(days=1))
    assert calls[0][0]["id"] == "late"
    with store() as session:
        assert as_dict(session.get(Digest, DAY.isoformat())) == before


@pytest.mark.parametrize(("total", "current_count", "old_count", "expected_current", "expected_old"), [
    (10, 20, 20, 8, 2), (35, 50, 20, 30, 5), (4, 10, 10, 3, 1),
    (2, 10, 10, 1, 1), (1, 10, 10, 1, 0), (1, 0, 10, 0, 1),
    (10, 2, 20, 2, 5), (10, 20, 1, 9, 1), (10, 0, 20, 0, 5),
    (10, 20, 0, 10, 0),
])
def test_current_priority_reserved_supplement_and_backfill_budgets(
    store, total, current_count, old_count, expected_current, expected_old,
):
    cfg = config(provider={"max_items": total})
    start, end = digest_window(DAY, cfg)
    with store.begin() as session:
        for i in range(current_count):
            article(session, f"current-{i:03}", end - timedelta(hours=1), score=10-i)
        for i in range(old_count):
            article(session, f"old-{i:03}", start - timedelta(hours=1), score=1000+i)
        result = selection(session, cfg)
    assert len(result.articles) == expected_current + expected_old <= total
    assert len(result.supplemental_ids) == expected_old <= MAX_SUPPLEMENTAL_ITEMS
    assert [row["supplemental"] for row in result.articles] == [False]*expected_current + [True]*expected_old


def test_unreported_capacity_carries_forward_and_history_only_counts_actual_citations(store):
    cfg = config(provider={"max_items": 2})
    start, end = digest_window(DAY, cfg)
    with store.begin() as session:
        article(session, "already-cited", start-timedelta(hours=2))
        article(session, "supplied-but-omitted", start-timedelta(hours=1))
        article(session, "never-selected", start-timedelta(hours=3))
        published(session, DAY-timedelta(days=1), cfg, ["already-cited"], count=2)
        first = selection(session, cfg)
        assert {row["id"] for row in first.articles} == {"supplied-but-omitted", "never-selected"}
        published(session, DAY, cfg, ["supplied-but-omitted"], count=2)
        # A provider omission remains eligible rather than being silently
        # acknowledged merely because it used one of the input slots.
        following = selection(session, cfg, DAY+timedelta(days=1))
        assert [row["id"] for row in following.articles] == ["never-selected"]


def test_window_boundaries_history_canonical_dedup_and_current_alias_precedence(store):
    cfg = config(lookback_hours=48)
    start, end = digest_window(DAY, cfg)
    cutoff = end-timedelta(hours=48)
    with store.begin() as session:
        article(session, "expired", cutoff-timedelta(seconds=1))
        article(session, "boundary-old", cutoff)
        article(session, "boundary-current", start)
        article(session, "future", end)
        article(session, "reported", cutoff, url="https://example.org/known")
        article(session, "reported-alias", start, url="https://example.org/known")
        article(session, "old-alias", cutoff, url="https://example.org/current-alias", score=1000)
        article(session, "current-alias", start, url="https://example.org/current-alias")
        article(session, "current-duplicate", start, url="https://example.org/current-alias", score=1)
        published(session, DAY-timedelta(days=1), cfg, ["reported"])
        result = selection(session, cfg)
    assert {row["id"] for row in result.articles} == {"boundary-old", "boundary-current", "current-alias"}
    assert result.supplemental_ids == {"boundary-old"}


def test_force_rebuild_can_reuse_own_sources_but_not_another_edition_citations(store):
    cfg = config()
    start, end = digest_window(DAY, cfg)
    with store.begin() as session:
        article(session, "own", end-timedelta(hours=1))
        article(session, "other", start-timedelta(hours=1))
        published(session, DAY, cfg, ["own"])
        published(session, DAY-timedelta(days=1), cfg, ["other"])
        assert [row["id"] for row in selection(session, cfg).articles] == ["own"]


@pytest.mark.asyncio
async def test_future_supplemental_citation_does_not_block_forced_historical_rebuild(store, monkeypatch):
    cfg = config()
    start, end = digest_window(DAY, cfg)
    pipeline = Pipeline(store, cfg)
    calls = provider_spy(monkeypatch)
    with store.begin() as session:
        article(session, "original", end-timedelta(hours=2))
        article(session, "late", end-timedelta(hours=1), collected=end+timedelta(hours=2))
        article(session, "previously-reported", start-timedelta(hours=1))
        published(session, DAY-timedelta(days=1), cfg, ["previously-reported"])
        published(session, DAY, cfg, ["original"])
        later = published(session, DAY+timedelta(days=1), cfg, ["late"])
        later.stories = mark_supplemental_stories(
            [story(["late"])], DigestSelection(articles=[{"id": "late"}], supplemental_ids={"late"}),
        )
        later_before = deepcopy(as_dict(later))
    await pipeline.digest(DAY, force=True)
    assert {row["id"] for row in calls[0]} == {"original", "late"}
    assert all(not row["supplemental"] for row in calls[0])
    with store() as session:
        assert as_dict(session.get(Digest, (DAY+timedelta(days=1)).isoformat())) == later_before
        rebuilt = session.get(Digest, DAY.isoformat())
        assert rebuilt.source_count == 2
        assert {uid for row in rebuilt.stories for uid in row["source_ids"]} == {"original", "late"}


@pytest.mark.parametrize(("ids", "prefix"), [
    (["current"], ""), (["old"], "补充｜"), (["old", "current"], "含补充｜"),
])
def test_maximum_model_title_is_preserved_in_explicit_display_contract(ids, prefix):
    original = "题" * 160
    chosen = DigestSelection(articles=[{"id": "current"}, {"id": "old"}], supplemental_ids={"old"})
    source = story(ids, original)
    [row] = mark_supplemental_stories([source], chosen)
    assert row["title"] == prefix + original
    assert len(row["title"]) == 160 + len(prefix) <= 164
    assert StoredDigestStory.model_validate(row).title == prefix + original
    assert source.title == original
    assert Story.model_json_schema()["properties"]["title"]["maxLength"] == 160
    assert StoredDigestStory.model_json_schema()["properties"]["title"]["maxLength"] == 164
    with pytest.raises(ValidationError):
        story(ids, original + "题")
    with pytest.raises(ValidationError):
        StoredDigestStory.model_validate({**row, "title": "题" * 165})
    # The display schema cannot be used to smuggle an oversized model title.
    with pytest.raises(ValidationError):
        mark_supplemental_stories([source.model_copy(update={"title": "题" * 161})], chosen)


def test_labels_follow_actual_citations_with_untrusted_flags_and_titles_overridden():
    chosen = DigestSelection(articles=[{"id": "current"}, {"id": "old"}], supplemental_ids={"old"})
    stories = [story(["current"], "含补充｜A"), story(["old", "old"], "补充｜B"),
               story(["old", "current"], "C")]
    rows = mark_supplemental_stories(stories, chosen)
    assert [row["title"] for row in rows] == ["A", "补充｜B", "含补充｜C"]
    assert [row["supplemental"] for row in rows] == [False, True, True]
    assert [row["supplemental_source_ids"] for row in rows] == [[], ["old"], ["old"]]
    assert stories[0].title == "含补充｜A"
    with pytest.raises(ValueError, match="outside the supplied evidence"):
        mark_supplemental_stories([story(["not-selected"])], chosen)


@pytest.mark.asyncio
async def test_mixed_story_is_marked_by_pipeline_and_unused_supplement_does_not_mark_current(store, monkeypatch):
    cfg = config()
    start, end = digest_window(DAY, cfg)
    pipeline = Pipeline(store, cfg)
    provider_spy(monkeypatch, lambda _: [["current", "old"], ["current"]])
    with store.begin() as session:
        article(session, "current", end-timedelta(hours=1))
        article(session, "old", start-timedelta(hours=1))
    await pipeline.digest(DAY)
    with store() as session:
        rows = session.get(Digest, DAY.isoformat()).stories
    assert rows[0]["title"].startswith("含补充｜") and rows[0]["supplemental_source_ids"] == ["old"]
    assert not rows[1]["title"].startswith(("含补充｜", "补充｜")) and not rows[1]["supplemental"]


@pytest.mark.asyncio
async def test_reading_disabled_never_fetches_ephemeral_publisher_text(store, monkeypatch):
    cfg = config(enrich_official_articles=True)
    pipeline = Pipeline(store, cfg)
    calls = provider_spy(monkeypatch)

    def no_network(*args, **kwargs):
        pytest.fail("Disabled reading must not fetch unsaved publisher text")

    monkeypatch.setattr(httpx.AsyncClient, "stream", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "request", no_network)
    _, end = digest_window(DAY, cfg)
    with store.begin() as session:
        original = article(session, "official", end-timedelta(hours=1),
                           url="https://openai.com/index/synthetic-fixture")
        expected = original.text
    await pipeline.digest(DAY)
    assert calls[0][0]["text"] == expected
    assert "resources" not in calls[0][0]
