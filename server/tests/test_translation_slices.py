"""Cooperative translation slices persist progress without cancelling model calls."""

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import event, select

from radar.config import TranslationConfig
from radar.db import database
from radar.models import Article, ArticleTranslation, Translation
from radar.translation import TranslationService, ensure_translation

SOURCE = "This synthetic AI model supports research."
CHINESE = "这款合成人工智能模型支持研究。"
SERVER = Path(__file__).resolve().parents[1]

WORKER = r'''
import asyncio, json, os, sys
from pathlib import Path
from radar.config import TranslationConfig
from radar.db import database
from radar.translation import TranslationService

engine, sessions = database(sys.argv[1])
service = TranslationService(sessions, TranslationConfig(enabled=True, concurrency=1))
async def completion(payload, *, system, model, stage):
    with Path(sys.argv[3]).open("a") as output:
        output.write(json.dumps({"stage": stage}) + "\n")
        output.flush()
        os.fsync(output.fileno())
    if stage == "audit" and sys.argv[4] == "crash":
        os._exit(77)
    parts = payload["untrusted_parts"]
    if stage == "audit":
        return json.dumps({"audits": [{"id": p["id"], "approved": True, "issues": []} for p in parts]})
    return json.dumps({"translations": [{"id": p["id"], "zh": "这款合成人工智能模型支持研究。",
        "approved": True, "issues": []} for p in parts]})
service._completion = completion
asyncio.run(service.translate_one(sys.argv[2], max_stage_calls=1))
engine.dispose()
'''


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-only")
    url = f"sqlite:///{tmp_path}/slices.db"
    engine, sessions = database(url)
    config = TranslationConfig(enabled=True, concurrency=1)
    yield sessions, config, url, tmp_path
    engine.dispose()


def row_snapshot(sessions, key):
    with sessions() as session:
        row = session.get(Translation, key)
        return deepcopy({column.name: getattr(row, column.name) for column in row.__table__.columns})


def create_cache(sessions, config, source=SOURCE):
    with sessions.begin() as session:
        return ensure_translation(session, source, source, config).id


def run_worker(url, key, log, *, crash=False):
    result = subprocess.run(
        [sys.executable, "-B", "-c", WORKER, url, key, str(log), "crash" if crash else "complete"],
        cwd=SERVER, env={**os.environ, "PYTHONPATH": str(SERVER), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == (77 if crash else 0), result.stderr


def logged_stages(log):
    return [json.loads(line)["stage"] for line in log.read_text().splitlines()]


def fake_completion(calls, *, reject=False):
    async def completion(service, payload, *, system, model, stage):
        parts = payload["untrusted_parts"]
        calls.append((stage, [part["source"] for part in parts]))
        if stage == "audit":
            return json.dumps({"audits": [{"id": part["id"], "approved": not reject,
                "issues": ["候选遗漏来源限定。"] if reject else []} for part in parts]})
        return json.dumps({"translations": [{"id": part["id"], "zh": CHINESE,
            "approved": True, "issues": []} for part in parts]})
    return completion


@pytest.mark.asyncio
async def test_stage_boundary_yields_resume_in_new_processes_without_duplicate_calls_or_retry_cost(store):
    sessions, config, url, directory = store
    key = create_cache(sessions, config)
    log = directory / "calls.jsonl"
    for stage, expected_status, attempts in [("draft", "pending", 0), ("correction", "pending", 0),
                                              ("audit", "ready", 1)]:
        run_worker(url, key, log)
        state = row_snapshot(sessions, key)
        assert state["status"] == expected_status and state["attempts"] == attempts
        assert state["owner"] == state["lease_until"] == state["retry_at"] == ""
        assert state["original_title"] == state["original_text"] == SOURCE
        part = state["parts"][0]
        assert part["draft"] == part["initial_draft"] == CHINESE
        history = part["workflow_history"]
        reserved = [record for record in history if record["kind"] == "reserved"]
        results = [record for record in history if record["kind"] == "result"]
        assert len(reserved) == len(results) == len(logged_stages(log))
        assert {record["call_id"] for record in reserved} == {record["call_id"] for record in results}
        assert results[-1]["stage"] == stage and results[-1]["outcome"] == "completed"
        if stage != "audit":
            assert state["text_zh"] == "" and "audit" not in part
    assert logged_stages(log) == ["draft", "correction", "audit"]
    before = row_snapshot(sessions, key)
    run_worker(url, key, log)
    assert row_snapshot(sessions, key) == before and logged_stages(log) == ["draft", "correction", "audit"]


@pytest.mark.asyncio
async def test_unknown_audit_outcome_remains_held_after_sliced_process_crash(store, monkeypatch):
    sessions, config, url, directory = store
    key = create_cache(sessions, config)
    log = directory / "calls.jsonl"
    run_worker(url, key, log)
    run_worker(url, key, log)
    run_worker(url, key, log, crash=True)
    with sessions.begin() as session:
        row = session.get(Translation, key)
        assert row.status == "running"
        row.lease_until = "2000-01-01"
    before = row_snapshot(sessions, key)

    async def forbidden(*args, **kwargs):
        pytest.fail("A new slice cannot replay an unknown model request")

    monkeypatch.setattr(TranslationService, "_completion", forbidden)
    for force in (False, True):
        await TranslationService(sessions, config).translate_one(key, force=force, max_stage_calls=1)
        assert row_snapshot(sessions, key) == before
    assert logged_stages(log) == ["draft", "correction", "audit"]
    assert before["text_zh"] == "" and before["parts"][0]["draft"] == CHINESE


@pytest.mark.asyncio
async def test_limited_slices_rotate_long_document_behind_other_waiting_cache(store, monkeypatch):
    sessions, config, _, _ = store
    long_source = SOURCE * 230
    short_source = "This other synthetic AI model supports research."
    keys = [create_cache(sessions, config, source) for source in (long_source, short_source)]
    with sessions.begin() as session:
        for index, (key, source) in enumerate(zip(keys, (long_source, short_source), strict=True)):
            article = Article(id=str(index), platform="x", source_id="fixture", external_id=str(index),
                              url=f"https://example.org/{index}", canonical_url=f"https://example.org/{index}",
                              title=source, text=source, author="Synthetic researcher", published_at="2020-01-01")
            session.add(article)
            session.flush()
            session.add(ArticleTranslation(article_id=article.id, translation_id=key))
            session.get(Translation, key).updated_at = f"200{index}-01-01"
    calls = []
    monkeypatch.setattr(TranslationService, "_completion", fake_completion(calls))
    await TranslationService(sessions, config).pending(limit=1, max_stage_calls=1)
    first = row_snapshot(sessions, keys[0])
    assert len(first["parts"]) >= 3 and first["status"] == "pending" and first["attempts"] == 0
    assert len(calls) == 1 and calls[0][0] == "draft" and short_source not in calls[0][1]
    await TranslationService(sessions, config).pending(limit=1, max_stage_calls=1)
    assert calls[1] == ("draft", [short_source]) and len(calls) == 2
    assert row_snapshot(sessions, keys[0]) == first
    assert row_snapshot(sessions, keys[1])["parts"][0]["draft"] == CHINESE


@pytest.mark.asyncio
async def test_semantic_denial_is_not_resampled_across_slices_or_force(store, monkeypatch):
    sessions, config, _, _ = store
    key = create_cache(sessions, config)
    calls = []
    monkeypatch.setattr(TranslationService, "_completion", fake_completion(calls, reject=True))
    for _ in range(6):
        await TranslationService(sessions, config).translate_one(key, force=True, max_stage_calls=1)
    assert [stage for stage, _ in calls] == ["draft", "correction", "audit", "correction"]
    state = row_snapshot(sessions, key)
    assert state["status"] == "review_required" and state["text_zh"] == ""
    assert len([record for record in state["parts"][0]["quality_history"] if record["kind"] == "audit"]) == 1


@pytest.mark.asyncio
async def test_read_only_evidence_never_creates_reconciles_or_invokes_models(store, monkeypatch):
    sessions, config, _, _ = store
    ready = create_cache(sessions, config)
    pending_source = "This pending AI model supports research."
    pending = create_cache(sessions, config, pending_source)
    with sessions.begin() as session:
        row = session.get(Translation, ready)
        row.status, row.title_zh, row.text_zh = "ready", CHINESE, CHINESE
    before = {key: row_snapshot(sessions, key) for key in (ready, pending)}

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only cache evidence cannot read keys or start translation work")

    monkeypatch.setattr("radar.translation.secret", forbidden)
    monkeypatch.setattr("radar.translation.ensure_translation", forbidden)
    monkeypatch.setattr(TranslationService, "reconcile_machine_checks", forbidden)
    monkeypatch.setattr(TranslationService, "_completion", forbidden)
    statements = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.split()[0].upper())

    engine = sessions.kw["bind"]
    inputs = [{"title": source, "text": source} for source in (SOURCE, pending_source, "A missing synthetic source.")]
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        output = await TranslationService(sessions, config).evidence(inputs, force=True, translate=False)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    assert output == [{**inputs[0], "title_zh": CHINESE, "text_zh": CHINESE}, inputs[1], inputs[2]]
    assert not ({"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"} & set(statements))
    with sessions() as session:
        assert set(session.scalars(select(Translation.id))) == {ready, pending}
    assert {key: row_snapshot(sessions, key) for key in before} == before
