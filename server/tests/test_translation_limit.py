import asyncio
import copy
import json
import sys
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from openai import APIStatusError
from sqlalchemy import select

from radar import cli
from radar.config import RadarConfig, TranslationConfig
from radar.db import database
from radar.models import Article, ArticleDocument, ArticleTranslation, Job, Translation, WebDocument
from radar.pipeline import ingest
from radar.schemas import IncomingArticle
from radar.translation import (
    RECHECK_POLICY,
    AuditedPart,
    TranslatedPart,
    TranslationService,
    cache_key,
    candidate_fingerprint,
    ensure_translation,
)
from radar.translation_recheck import translate_limited

MAIN = "AI agents are useful."
FIRST = "AI models can help researchers."
SECOND = "AI tools support science."
ZH = {MAIN: "智能体很有用。", FIRST: "人工智能模型可以帮助研究人员。", SECOND: "人工智能工具支持科学研究。"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    engine, sessions = database(f"sqlite:///{tmp_path}/limit.db")
    yield sessions
    engine.dispose()


def snapshot(sessions, model):
    with sessions() as session:
        return {row.id: copy.deepcopy({c.name: getattr(row, c.name) for c in model.__table__.columns})
                for row in session.scalars(select(model))}


def seed(sessions, config, *, ready_main=True):
    with sessions.begin() as session:
        ingest(session, [IncomingArticle(
            platform="x", external_id="main", title=MAIN, text=MAIN, author="OpenAI", handle="OpenAI",
            url="https://x.com/OpenAI/status/main", published_at=datetime.now(UTC),
        )], RadarConfig(translation=config))
        article = session.scalar(select(Article))
        row = session.get(Translation, session.get(ArticleTranslation, article.id).translation_id)
        if ready_main:
            row.status, row.title_zh, row.text_zh = "ready", ZH[MAIN], ZH[MAIN]
        for uid, text in [("a-first", FIRST), ("b-second", SECOND)]:
            session.add(WebDocument(id=uid, url=f"https://example.org/{uid}", title=text, text=text))
            session.flush()
            session.add(ArticleDocument(article_id=article.id, document_id=uid, relation="link"))
            ensure_translation(session, text, text, config)
    return {text: cache_key(text, text, config) for text in ZH}


@pytest.fixture
def model(monkeypatch):
    calls = []

    async def request(service, parts, *, review):
        calls.extend(("review" if review else "draft", part["source"]) for part in parts)
        return {part["id"]: TranslatedPart(id=part["id"], zh=ZH[part["source"]], approved=True)
                for part in parts}

    async def audit(service, parts):
        calls.extend(("audit", part["source"]) for part in parts)
        return {part["id"]: AuditedPart(id=part["id"], approved=True) for part in parts}

    monkeypatch.setattr(TranslationService, "request", request)
    monkeypatch.setattr(TranslationService, "audit", audit)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,requested", [(100, 1), (1, 500)])
async def test_limit_uses_one_real_cache_preserves_config_sources_and_other_caches(store, model, configured, requested):
    config = TranslationConfig(enabled=True, max_documents=configured)
    keys = seed(store, config)
    config_before = config.model_dump()
    before = snapshot(store, Translation)
    articles, documents = snapshot(store, Article), snapshot(store, WebDocument)

    result = await translate_limited(store, config, limit=requested)

    assert model == [(stage, FIRST) for stage in ["draft", "review", "audit"]]
    assert result["status"] == "completed" and result["limit"] == 1
    assert result["counts"] == {"ready": 1} and result["resource_counts"] == {"ready": 1, "pending": 1}
    assert config.model_dump() == config_before
    assert {text: cache_key(text, text, config) for text in ZH} == keys
    after = snapshot(store, Translation)
    assert after[keys[MAIN]] == before[keys[MAIN]] and after[keys[SECOND]] == before[keys[SECOND]]
    assert after[keys[FIRST]]["status"] == "ready" and after[keys[FIRST]]["attempts"] == 1
    assert snapshot(store, Article) == articles and snapshot(store, WebDocument) == documents
    job = snapshot(store, Job)[result["job_id"]]
    assert job["kind"] == "translate" and job["status"] == "completed" and job["finished_at"]
    assert "网页正文中文版本 1 份，1 份仍在等待翻译或校对" in job["message"]


@pytest.mark.asyncio
async def test_existing_running_job_rejected_unchanged_before_any_translation(store, model):
    config = TranslationConfig(enabled=True)
    seed(store, config)
    with store.begin() as session:
        session.add(Job(kind="read", message="Existing active reading task"))
    jobs, caches = snapshot(store, Job), snapshot(store, Translation)

    with pytest.raises(RuntimeError, match="已有任务"):
        await translate_limited(store, config, limit=1, force=True)

    assert snapshot(store, Job) == jobs and snapshot(store, Translation) == caches and model == []


@pytest.mark.asyncio
async def test_force_resumes_exhausted_draft_but_keeps_live_lease_and_ready_cache(store, model):
    config = TranslationConfig(enabled=True)
    keys = seed(store, config)
    with store.begin() as session:
        leased = session.get(Translation, keys[FIRST])
        leased.lease_until = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        leased.owner = "another-worker"
        row = session.get(Translation, keys[SECOND])
        row.parts = [{"id": "body-0", "source": SECOND, "draft": ZH[SECOND], "zh": ZH[SECOND],
                      "ok": False, "correction_required": False}]

    async def timeout(parts):
        raise TimeoutError("synthetic completed audit timeout")

    failure_service = TranslationService(store, config)
    failure_service.audit = timeout
    await failure_service.translate_one(keys[SECOND])
    with store.begin() as session:
        row = session.get(Translation, keys[SECOND])
        assert row.status == "error"
        assert row.parts[0]["workflow_history"][-1]["outcome"] == "known_transport"
        row.attempts = config.max_attempts
        row.retry_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    before = snapshot(store, Translation)
    unforced = await translate_limited(store, config, limit=1)
    assert model == [] and unforced["resource_counts"] == {"pending": 1, "error": 1}
    assert snapshot(store, Translation) == before

    result = await translate_limited(store, config, limit=1, force=True)

    assert model == [("audit", SECOND)] and result["resource_counts"] == {"pending": 1, "ready": 1}
    after = snapshot(store, Translation)
    assert after[keys[MAIN]] == before[keys[MAIN]] and after[keys[FIRST]] == before[keys[FIRST]]
    assert after[keys[SECOND]]["attempts"] == config.max_attempts + 1
    part = after[keys[SECOND]]["parts"][0]
    assert part["draft"] == ZH[SECOND] and part["audit"]["policy"] == RECHECK_POLICY
    assert part["audit"]["fingerprint"] == candidate_fingerprint(SECOND, ZH[SECOND])


@pytest.mark.asyncio
async def test_limited_402_keeps_balance_alert_and_attempts_without_fallback(store, monkeypatch):
    config = TranslationConfig(enabled=True)
    keys = seed(store, config)
    before = snapshot(store, Translation)
    calls = []

    async def depleted(service, parts, *, review):
        calls.append(parts[0]["source"])
        raise APIStatusError("private-provider-detail", response=httpx.Response(
            402, request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        ), body={})

    monkeypatch.setattr(TranslationService, "request", depleted)
    monkeypatch.setattr(TranslationService, "audit", lambda *args: pytest.fail("402 must not fall back"))

    result = await translate_limited(store, config, limit=1, force=True)

    assert calls == [FIRST] and result["alert"]["code"] == "insufficient_balance"
    assert result["resource_counts"] == {"insufficient_balance": 1, "pending": 1}
    after = snapshot(store, Translation)
    assert after[keys[FIRST]]["attempts"] == 0
    assert after[keys[FIRST]]["owner"] == "" and after[keys[FIRST]]["lease_until"] == ""
    assert after[keys[SECOND]] == before[keys[SECOND]] and after[keys[MAIN]] == before[keys[MAIN]]
    assert "private-provider-detail" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("private-provider-detail"), asyncio.CancelledError()])
async def test_unexpected_failure_finishes_only_owned_job_with_safe_message(store, monkeypatch, failure):
    config = TranslationConfig(enabled=True)
    seed(store, config)
    caches = snapshot(store, Translation)

    async def broken(service, force=False):
        raise failure

    monkeypatch.setattr(TranslationService, "pending", broken)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await translate_limited(store, config, limit=1)
    else:
        result = await translate_limited(store, config, limit=1)
        assert result["status"] == "failed"
    jobs = list(snapshot(store, Job).values())
    assert len(jobs) == 1 and jobs[0]["status"] == "failed" and jobs[0]["finished_at"]
    assert "private-provider-detail" not in jobs[0]["message"]
    assert "网页正文中文版本 0 份，2 份仍在等待翻译或校对" in jobs[0]["message"]
    assert snapshot(store, Translation) == caches


@pytest.mark.parametrize("args", [
    ["collect", "--limit", "1"], ["init", "--limit", "1"],
    ["translate", "--limit", "0"], ["translate", "--limit", "501"],
    ["translate", "--limit", "1.5"], ["translate", "--limit", "1", "--file", "replacement.txt"],
    ["translate", "--limit", "1", "--date", "2026-09-08"],
    ["translate", "--limit", "1", "--recheck-article", "an-id"],
    ["translate", "--limit", "1", "--recheck-editorial"],
])
def test_invalid_limit_cli_combinations_rejected_before_configuration(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["radar", *args])
    monkeypatch.setattr(cli, "Settings", lambda: pytest.fail("Invalid CLI must not load configuration"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_cli_limited_translation_uses_real_queue_without_initializing_pipeline(store, model, monkeypatch, capsys):
    config = RadarConfig(translation=TranslationConfig(enabled=True))
    keys = seed(store, config.translation, ready_main=False)

    class Configured:
        database_url = str(store.kw["bind"].url)

        def load(self):
            return config

    monkeypatch.setattr(cli, "Settings", Configured)
    monkeypatch.setattr(cli, "Pipeline", lambda *args: pytest.fail("Limit must not initialize Pipeline"))
    monkeypatch.setattr(sys, "argv", ["radar", "translate", "--limit", "1", "--force"])
    cli.main()

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed" and result["limit"] == 1
    assert model == [(stage, MAIN) for stage in ["draft", "review", "audit"]]
    assert result["counts"] == {"ready": 1} and result["resource_counts"] == {"pending": 2}
    assert snapshot(store, Translation)[keys[MAIN]]["status"] == "ready"
