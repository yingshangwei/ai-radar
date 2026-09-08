import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from radar import cli
from radar import translation_recheck as rechecks
from radar.config import RadarConfig, TranslationConfig
from radar.db import database
from radar.models import Article, ArticleTranslation, Job, Translation
from radar.pipeline import ingest
from radar.schemas import IncomingArticle
from radar.translation import AuditedPart, cache_key


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    engine, sessions = database(f"sqlite:///{tmp_path}/recheck.db")
    yield sessions
    engine.dispose()


def cached_article(sessions, external_id, *, text="AI agents are useful.", editorial=None, reviewed=False):
    config = RadarConfig(translation=TranslationConfig(enabled=True))
    with sessions.begin() as session:
        ingest(session, [IncomingArticle(
            platform="x", external_id=external_id, title=text, text=text,
            url=f"https://x.com/OpenAI/status/{external_id}", author="OpenAI", handle="OpenAI",
            published_at=datetime.now(UTC),
        )], config)
        article = session.scalar(select(Article).where(Article.external_id == external_id))
        row = session.get(Translation, session.get(ArticleTranslation, article.id).translation_id)
        row.status, row.title_zh, row.text_zh = "ready", "智能体很有用。", "智能体很有用。"
        row.model, row.review_model = "machine-draft", "machine-review"
        part = {"id": "body-0", "source": text, "zh": row.text_zh, "draft": row.text_zh, "ok": True}
        if editorial:
            part.update(editorial)
            row.review_model += " + Codex editorial review"
        if reviewed:
            part["test_audited"] = True
        row.parts = [part]
        return article.id, row.id


def snapshot(sessions, key):
    with sessions() as session:
        row = session.get(Translation, key)
        return {c.name: getattr(row, c.name) for c in row.__table__.columns}


@pytest.fixture
def fake_reviewer(monkeypatch):
    calls = []

    def needs(row):
        return not all(part.get("test_audited") for part in row.parts)

    async def translate(service, key, force=False, *, recheck=False):
        calls.append((key, force, recheck))
        with service.sessions.begin() as session:
            row = session.get(Translation, key)
            row.parts = [{"id": "body-0", "source": row.original_text, "test_audited": True}]
            row.review_model = "machine-review"
            row.status = "ready"

    monkeypatch.setattr(rechecks, "needs_recheck", needs)
    monkeypatch.setattr(rechecks.TranslationService, "translate_one", translate)
    return calls


def test_editorial_selection_excludes_machine_caches_and_archived_metadata(store):
    config = TranslationConfig(enabled=True)
    _, old = cached_article(store, "1", editorial={"editorial_previous_zh": "旧候选"})
    _, weather = cached_article(store, "2", text="AI weather model", editorial={"editorial_previous": "旧候选"})
    _, untouched = cached_article(store, "3", text="AI machine-reviewed cache")
    _, archived = cached_article(store, "4", text="AI audited cache")
    with store.begin() as session:
        row = session.get(Translation, archived)
        row.parts = [{"id": "body-0", "review_history": [{"editorial_note": "retained history"}]}]
    with store() as session:
        selected = rechecks.select_rechecks(session, config, editorial=True)
        assert {row.id for row in selected} == {old, weather}
        assert untouched not in {row.id for row in selected}


def test_editorial_selection_resumes_only_pending_legacy_rechecks(store):
    _, legacy = cached_article(store, "1")
    _, machine = cached_article(store, "2", text="AI machine cache")
    with store.begin() as session:
        for key in [legacy, machine]:
            row = session.get(Translation, key)
            row.status = "review_required"
            row.parts = [{
                "id": "body-0", "recheck_pending": True,
                "review_history": [{
                    "previous": {},
                    "row_provenance": {"review_model": "Codex editorial review" if key == legacy else "machine"},
                }],
            }]
    with store() as session:
        selected = rechecks.select_rechecks(session, TranslationConfig(enabled=True), editorial=True)
        assert [row.id for row in selected] == [legacy]


@pytest.mark.asyncio
async def test_cli_orchestrator_uses_real_recheck_audit_path(store, monkeypatch):
    article, key = cached_article(store, "1", editorial={"editorial_previous_zh": "智能体很有用。"})
    calls = []

    async def audit(service, parts):
        calls.extend(parts)
        return {p["id"]: AuditedPart(id=p["id"], approved=True) for p in parts}

    async def unexpected_request(*args, **kwargs):
        pytest.fail("Existing machine candidate should be audited without redrafting")

    monkeypatch.setattr(rechecks.TranslationService, "audit", audit)
    monkeypatch.setattr(rechecks.TranslationService, "request", unexpected_request)
    config = TranslationConfig(enabled=True)
    first = await rechecks.recheck_translations(store, config, article_ids=[article])
    assert first["status"] == "completed" and len(calls) == 1
    assert calls[0]["candidate"] == "智能体很有用。"
    completed = snapshot(store, key)
    again = await rechecks.recheck_translations(store, config, article_ids=[article])
    assert again["skipped"] == 1 and len(calls) == 1 and snapshot(store, key) == completed
    forced = await rechecks.recheck_translations(store, config, article_ids=[article], force=True)
    assert forced["status"] == "completed" and len(calls) == 1
    assert snapshot(store, key) == completed  # Force cannot resample an already approved exact candidate.
    with store() as session:
        assert not rechecks.has_editorial_provenance(session.get(Translation, key))


@pytest.mark.asyncio
async def test_recheck_deduplicates_bound_caches_and_preserves_unrelated_rows(store, fake_reviewer):
    first, key = cached_article(store, "1")
    twin, twin_key = cached_article(store, "2")
    _, other = cached_article(store, "3", text="Another AI model announcement")
    before = snapshot(store, other)
    config = TranslationConfig(enabled=True)

    result = await rechecks.recheck_translations(store, config, article_ids=[first, twin, first])

    assert key == twin_key and result["selected"] == 1 and result["status"] == "completed"
    assert fake_reviewer == [(key, False, True)]
    assert snapshot(store, other) == before
    current = snapshot(store, key)
    second = await rechecks.recheck_translations(store, config, article_ids=[first])
    assert second["skipped"] == 1 and len(fake_reviewer) == 1
    assert snapshot(store, key) == current
    forced = await rechecks.recheck_translations(store, config, article_ids=[first], force=True)
    assert forced["skipped"] == 0 and fake_reviewer[-1] == (key, True, True)
    with store() as session:
        assert session.get(Job, result["job_id"]).status == "completed"


@pytest.mark.asyncio
async def test_editorial_recheck_is_automatic_and_does_not_select_history_again(store, fake_reviewer):
    _, legacy = cached_article(store, "1", editorial={"editorial_previous_zh": "原机器候选"})
    _, untouched = cached_article(store, "2", text="AI untouched cache")
    before = snapshot(store, untouched)
    result = await rechecks.recheck_translations(store, TranslationConfig(enabled=True), editorial=True)
    assert result["translation_ids"] == [legacy] and result["status"] == "completed"
    second = await rechecks.recheck_translations(store, TranslationConfig(enabled=True), editorial=True)
    assert second["selected"] == 0 and len(fake_reviewer) == 1
    assert snapshot(store, untouched) == before


@pytest.mark.asyncio
async def test_busy_job_is_never_cancelled_by_recheck(store, fake_reviewer):
    article, key = cached_article(store, "1")
    with store.begin() as session:
        existing = Job(kind="collect", status="running", message="Existing task")
        session.add(existing)
        session.flush()
        uid = existing.id
    before = snapshot(store, key)
    with pytest.raises(RuntimeError, match="已有任务"):
        await rechecks.recheck_translations(store, TranslationConfig(enabled=True), article_ids=[article])
    with store() as session:
        job = session.get(Job, uid)
        assert job.status == "running" and job.message == "Existing task"
        assert len(session.scalars(select(Job)).all()) == 1
    assert snapshot(store, key) == before and fake_reviewer == []


@pytest.mark.asyncio
async def test_active_translation_lease_is_not_overridden_even_with_force(store, fake_reviewer):
    article, key = cached_article(store, "1")
    with store.begin() as session:
        row = session.get(Translation, key)
        row.lease_until = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        row.owner = "other-worker"
    before = snapshot(store, key)
    with pytest.raises(RuntimeError, match="其他任务处理"):
        await rechecks.recheck_translations(
            store, TranslationConfig(enabled=True), article_ids=[article], force=True
        )
    assert snapshot(store, key) == before and fake_reviewer == []


@pytest.mark.asyncio
async def test_missing_credentials_do_not_change_selected_cache_or_create_job(store, monkeypatch, fake_reviewer):
    article, key = cached_article(store, "1")
    before = snapshot(store, key)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(ValueError, match="尚未配置凭据"):
        await rechecks.recheck_translations(store, TranslationConfig(enabled=True), article_ids=[article])
    with store() as session:
        assert session.scalar(select(Job)) is None
    assert snapshot(store, key) == before and fake_reviewer == []


@pytest.mark.asyncio
async def test_failed_review_is_visible_as_failed_job(store, monkeypatch, fake_reviewer):
    article, key = cached_article(store, "1")

    async def rejected(service, key, force=False, *, recheck=False):
        with service.sessions.begin() as session:
            row = session.get(Translation, key)
            row.status = "review_required"

    monkeypatch.setattr(rechecks.TranslationService, "translate_one", rejected)
    result = await rechecks.recheck_translations(store, TranslationConfig(enabled=True), article_ids=[article])
    assert result["status"] == "failed" and result["remaining"] == 1 and result["ready"] == 0
    with store() as session:
        assert session.get(Job, result["job_id"]).finished_at
        assert session.get(Translation, key).status == "review_required"


@pytest.mark.asyncio
async def test_cancelled_recheck_closes_its_job_without_persisting_exception(store, monkeypatch, fake_reviewer):
    article, _ = cached_article(store, "1")

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError("private-provider-detail")

    monkeypatch.setattr(rechecks.TranslationService, "translate_one", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await rechecks.recheck_translations(store, TranslationConfig(enabled=True), article_ids=[article])
    with store() as session:
        job = session.scalar(select(Job))
        assert job.status == "failed" and job.finished_at
        assert "private-provider-detail" not in job.message


@pytest.mark.parametrize("args", [
    ["collect", "--recheck-editorial"],
    ["init", "--recheck-article", "an-id"],
    ["translate", "--recheck-editorial", "--file", "replacement.txt"],
    ["translate", "--recheck-editorial", "--recheck-article", "an-id"],
])
def test_cli_recheck_rejects_wrong_actions_and_text_inputs(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["radar", *args])
    monkeypatch.setattr(cli, "Settings", lambda: pytest.fail("Invalid CLI must not load configuration"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_cli_recheck_never_initializes_pipeline(store, monkeypatch, fake_reviewer, capsys):
    article, key = cached_article(store, "1")
    engine = store.kw["bind"]
    config = RadarConfig(translation=TranslationConfig(enabled=True))

    class Configured:
        database_url = str(engine.url)

        def load(self):
            return config

    monkeypatch.setattr(cli, "Settings", Configured)
    monkeypatch.setattr(cli, "Pipeline", lambda *args: pytest.fail("Recheck must not initialize Pipeline"))
    monkeypatch.setattr(sys, "argv", ["radar", "translate", "--recheck-article", article])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed" and result["translation_ids"] == [key]


def test_selection_rejects_unknown_or_stale_article_without_creating_cache(store):
    article, _ = cached_article(store, "1")
    changed = TranslationConfig(enabled=True, revision="changed-policy")
    with store() as session:
        with pytest.raises(ValueError, match="文章不存在"):
            rechecks.select_rechecks(session, changed, article_ids=["unknown"])
        with pytest.raises(ValueError, match="当前配置"):
            rechecks.select_rechecks(session, changed, article_ids=[article])
        assert session.get(Translation, cache_key("AI agents are useful.", "AI agents are useful.", changed)) is None
