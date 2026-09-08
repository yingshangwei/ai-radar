import asyncio
import copy
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from radar import cli, translation_recheck
from radar.config import RadarConfig, TranslationConfig
from radar.db import database
from radar.models import Article, ArticleTranslation, Job, Translation
from radar.translation import RECHECK_POLICY, TranslationService, candidate_fingerprint, ensure_translation
from radar.translation_recheck import translate_limited

SOURCE = "AI agents can assist research."
CHINESE = "人工智能智能体可以辅助研究。"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    engine, sessions = database(f"sqlite:///{tmp_path}/machine.db")
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append("model_or_translation_called")
        raise AssertionError("Machine revalidation must not start translation or a model")

    for method in ("request", "audit", "_completion", "translate_one", "auxiliary"):
        monkeypatch.setattr(TranslationService, method, forbidden)
    yield sessions, calls
    engine.dispose()


def seed(sessions, config, uid="first", *, text=SOURCE, chinese=CHINESE, approved=True):
    with sessions.begin() as session:
        session.add(Article(
            id=uid, platform="x", source_id="x", external_id=uid,
            url=f"https://example.org/{uid}", canonical_url=f"https://example.org/{uid}",
            title=text, text=text, author="Synthetic fixture", published_at=datetime.now(UTC).isoformat(),
        ))
        row = ensure_translation(session, text, text, config)
        session.add(ArticleTranslation(article_id=uid, translation_id=row.id))
        fingerprint = candidate_fingerprint(text, chinese)
        review = {"model": config.review_model, "policy": RECHECK_POLICY,
                  "fingerprint": fingerprint, "approved": approved, "issues": [], "round": 1}
        audit = {"model": config.audit_model or config.review_model, "policy": RECHECK_POLICY,
                 "fingerprint": fingerprint, "approved": True, "issues": [],
                 "machine_issues": ["历史机器规则检查"], "correction_issues": []}
        row.parts = [{"id": "body-0", "source": text, "draft": chinese, "zh": chinese,
                      "ok": False, "correction_required": True, "issues": ["历史机器规则检查"],
                      "review": review, "audit": audit,
                      "quality_history": [{"kind": "correction", **review}, {"kind": "audit", **audit}]}]
        row.status, row.issues = "review_required", ["历史机器规则检查"]
        row.attempts = config.max_attempts
        row.retry_at = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
        return row.id


def snapshot(sessions, model):
    with sessions() as session:
        return {row.id: copy.deepcopy({c.name: getattr(row, c.name) for c in model.__table__.columns})
                for row in session.scalars(select(model))}


def configured_cli(monkeypatch, sessions, config):
    class Configured:
        database_url = str(sessions.kw["bind"].url)

        def load(self):
            return RadarConfig(translation=config)

    monkeypatch.setattr(cli, "Settings", Configured)
    monkeypatch.setattr(cli, "Pipeline", lambda *a, **k: pytest.fail("Machine mode must not initialize Pipeline"))


def test_real_cli_reuses_audits_without_api_key_or_model_and_preserves_receipts(store, monkeypatch, capsys):
    sessions, calls = store
    config = TranslationConfig(enabled=True)
    key = seed(sessions, config)
    before = snapshot(sessions, Translation)[key]
    originals = snapshot(sessions, Article)
    configured_cli(monkeypatch, sessions, config)
    monkeypatch.setattr(translation_recheck, "secret", lambda *a: pytest.fail("No credential admission gate"))
    monkeypatch.setattr(sys, "argv", ["radar", "translate", "--revalidate-machine", "--limit", "1"])

    cli.main()

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed" and result["machine_only"] is True
    assert result["machine_revalidated"] == 1 and result["counts"] == {"ready": 1}
    assert "复用已有模型审核，未调用模型" in result["message"]
    after = snapshot(sessions, Translation)[key]
    assert after["status"] == "ready" and after["text_zh"] == CHINESE
    assert after["attempts"] == before["attempts"]
    for name in ("draft", "zh", "source", "review", "audit", "quality_history"):
        assert after["parts"][0][name] == before["parts"][0][name]
    assert snapshot(sessions, Article) == originals and calls == []
    jobs = snapshot(sessions, Job)
    assert len(jobs) == 1 and jobs[result["job_id"]]["finished_at"]


@pytest.mark.asyncio
async def test_machine_limit_respects_leases_and_config_cap_then_is_idempotent(store):
    sessions, calls = store
    config = TranslationConfig(enabled=True, max_documents=1)
    leased = seed(sessions, config)
    available = seed(sessions, config, "second", text="AI tools can assist science.", chinese="人工智能工具可以辅助科学研究。")
    with sessions.begin() as session:
        row = session.get(Translation, leased)
        row.owner = "another-worker"
        row.lease_until = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    before = snapshot(sessions, Translation)
    result = await translate_limited(sessions, config, limit=500, machine_only=True)
    assert result["limit"] == 1 and result["machine_revalidated"] == 1
    assert snapshot(sessions, Translation)[leased] == before[leased]
    assert snapshot(sessions, Translation)[available]["status"] == "ready"
    after = snapshot(sessions, Translation)
    repeated = await translate_limited(sessions, config, limit=1, machine_only=True)
    assert repeated["machine_revalidated"] == 0 and snapshot(sessions, Translation) == after
    assert calls == [] and config.max_documents == 1


@pytest.mark.asyncio
async def test_model_rejection_stays_pending_and_message_does_not_claim_new_approval(store):
    sessions, calls = store
    config = TranslationConfig(enabled=True)
    seed(sessions, config, approved=False)
    before = snapshot(sessions, Translation)
    result = await translate_limited(sessions, config, limit=1, machine_only=True)
    assert result["status"] == "completed" and result["machine_revalidated"] == 0
    assert result["counts"] == {"review_required": 1}
    assert "1 条仍在等待翻译或校对" in result["message"]
    assert "新模型审核" not in result["message"]
    assert snapshot(sessions, Translation) == before and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["running", "disabled"])
async def test_machine_mode_preserves_running_jobs_and_disabled_configuration(store, blocked):
    sessions, calls = store
    config = TranslationConfig(enabled=blocked != "disabled")
    if blocked == "running":
        with sessions.begin() as session:
            session.add(Job(kind="daily", message="已有任务"))
    before = snapshot(sessions, Job)
    with pytest.raises((ValueError, RuntimeError)):
        await translate_limited(sessions, config, limit=1, machine_only=True)
    assert snapshot(sessions, Job) == before and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"force": True}, {"errors_only": True}])
async def test_direct_machine_operation_rejects_conflicts_before_reservation(store, extra):
    sessions, calls = store
    with pytest.raises(ValueError, match="不能"):
        await translate_limited(sessions, TranslationConfig(enabled=True), limit=1, machine_only=True, **extra)
    assert snapshot(sessions, Job) == {} and calls == []


@pytest.mark.parametrize("args", [
    ["translate", "--revalidate-machine"],
    ["collect", "--revalidate-machine", "--limit", "1"],
    *[["translate", "--revalidate-machine", "--limit", "1", *extra] for extra in [
        ["--force"], ["--errors-only"], ["--recheck-editorial"], ["--recheck-article", "id"],
        ["--date", "2026-09-08"], ["--file", "replacement.txt"],
        ["--replacement-text", "new candidate"],
    ]],
    ["translate", "--revalidate-machine", "--limit", "0"],
    ["translate", "--revalidate-machine", "--limit", "501"],
])
def test_conflicting_and_unknown_machine_options_rejected_before_settings(monkeypatch, args):
    monkeypatch.setattr(cli, "Settings", lambda: pytest.fail("Invalid options must not load configuration"))
    monkeypatch.setattr(sys, "argv", ["radar", *args])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("SECRET source/candidate"), asyncio.CancelledError()])
async def test_machine_failure_audits_owned_job_without_raw_error(store, monkeypatch, failure):
    sessions, calls = store

    async def broken(service, *, machine_only):
        assert machine_only is True
        raise failure

    monkeypatch.setattr(TranslationService, "pending", broken)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await translate_limited(sessions, TranslationConfig(enabled=True), limit=1, machine_only=True)
    else:
        result = await translate_limited(sessions, TranslationConfig(enabled=True), limit=1, machine_only=True)
        assert result["status"] == "failed"
    jobs = list(snapshot(sessions, Job).values())
    assert len(jobs) == 1 and jobs[0]["status"] == "failed" and jobs[0]["finished_at"]
    assert "SECRET" not in jobs[0]["message"] and "机器校验复检任务中断" in jobs[0]["message"]
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_machine_count_is_not_echoed_in_result(store, monkeypatch):
    sessions, _ = store

    async def invalid(service, *, machine_only):
        return {"machine_revalidated": "SECRET provider output"}

    monkeypatch.setattr(TranslationService, "pending", invalid)
    result = await translate_limited(sessions, TranslationConfig(enabled=True), limit=1, machine_only=True)
    assert result["status"] == "failed" and result["machine_revalidated"] == 0
    assert "SECRET" not in json.dumps(result)


def test_existing_limited_cli_does_not_pass_new_keyword(store, monkeypatch, capsys):
    sessions, _ = store
    configured_cli(monkeypatch, sessions, TranslationConfig(enabled=True))

    async def old_signature(sessions, config, *, limit, force, errors_only):
        return {"status": "completed", "limit": limit}

    monkeypatch.setattr(translation_recheck, "translate_limited", old_signature)
    monkeypatch.setattr(sys, "argv", ["radar", "translate", "--limit", "1"])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"status": "completed", "limit": 1}
