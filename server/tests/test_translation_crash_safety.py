"""Real process death must not replay an unknown translation model request."""

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from radar.config import TranslationConfig
from radar.db import database
from radar.models import Translation
from radar.translation import (
    RECHECK_POLICY,
    TranslationService,
    candidate_fingerprint,
    ensure_translation,
)

SOURCE = "This synthetic AI model is not open source."
CANDIDATE = "这款合成人工智能模型并未开源。"
PREVIOUS = "这款合成人工智能模型已经开源。"
SERVER = Path(__file__).resolve().parents[1]

CRASHING_WORKER = r"""
import asyncio, json, os, sys
from pathlib import Path
from radar.config import TranslationConfig
from radar.db import database
from radar.translation import TranslationService

engine, sessions = database(sys.argv[1])
service = TranslationService(sessions, TranslationConfig(enabled=True, concurrency=1))

async def no_generation(*args, **kwargs):
    raise AssertionError('The saved synthetic candidate must be reused')

async def die_during_audit(parts):
    marker = Path(sys.argv[3])
    with marker.open('x') as output:
        json.dump({'audit_entered': True, 'part_count': len(parts)}, output)
        output.flush()
        os.fsync(output.fileno())
    os._exit(77)

service.request = no_generation
service.audit = die_during_audit
asyncio.run(service.translate_one(sys.argv[2]))
raise SystemExit('Audit was not reached')
"""


@pytest.fixture
def crash_case(tmp_path):
    url = "sqlite:///" + str(tmp_path / "synthetic-crash.db")
    engine, sessions = database(url)
    config = TranslationConfig(enabled=True, concurrency=1)
    with sessions.begin() as session:
        row = ensure_translation(session, SOURCE, SOURCE, config)
        parts = deepcopy(row.parts)
        assert len(parts) == 1
        review = {
            "model": config.review_model, "policy": RECHECK_POLICY,
            "fingerprint": candidate_fingerprint(SOURCE, CANDIDATE),
            "approved": True, "issues": [], "round": 1, "at": "2020-01-02T00:00:00+00:00",
        }
        history = [
            {
                "kind": "audit", "policy": RECHECK_POLICY, "model": config.review_model,
                "fingerprint": candidate_fingerprint(SOURCE, PREVIOUS), "approved": False,
                "issues": ["合成旧候选的否定含义有误"], "at": "2020-01-01T00:00:00+00:00",
            },
            {"kind": "correction", **review},
        ]
        parts[0].update(
            draft=CANDIDATE, initial_draft=PREVIOUS, zh=CANDIDATE,
            ok=False, correction_required=False, review=review, quality_history=history,
        )
        row.parts = parts
        key = row.id
    yield sessions, config, url, key, tmp_path / "worker-entered.json", deepcopy(parts[0])
    engine.dispose()


def assert_candidate_and_history_preserved(row, before):
    assert row.original_title == SOURCE and row.original_text == SOURCE
    assert row.status != "ready" and row.text_zh == "" and row.title_zh == ""
    part = row.parts[0]
    for name in ("source", "draft", "initial_draft", "zh", "review", "quality_history"):
        assert part[name] == before[name]


async def resume_without_model(sessions, config, key, force):
    calls = []

    async def unexpected(*args, **kwargs):
        calls.append("unexpected model replay")
        raise AssertionError("Unknown outcome must not invoke a provider again")

    service = TranslationService(sessions, config)
    service.request = unexpected
    service.audit = unexpected
    await service.translate_one(key, force=force)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_process_death_inside_audit_is_not_replayed_after_lease_expiry(crash_case, force):
    sessions, config, url, key, marker, before = crash_case
    env = {**os.environ, "PYTHONPATH": str(SERVER), "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run(
        [sys.executable, "-B", "-c", CRASHING_WORKER, url, key, str(marker)],
        cwd=SERVER, env=env, capture_output=True, text=True, timeout=20, check=False,
    )
    assert completed.returncode == 77, completed.stderr
    assert json.loads(marker.read_text()) == {"audit_entered": True, "part_count": 1}
    with sessions.begin() as session:
        row = session.get(Translation, key)
        assert row.status == "running" and row.owner and row.lease_until
        assert_candidate_and_history_preserved(row, before)
        # Only this isolated test database's clock fields move; the crashed request stays unknown.
        row.lease_until = "2000-01-01T00:00:00+00:00"
        row.retry_at = ""
    await resume_without_model(sessions, config, key, force)
    with sessions() as session:
        assert_candidate_and_history_preserved(session.get(Translation, key), before)


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_legacy_expired_running_without_request_receipt_is_not_assumed_retryable(crash_case, force):
    sessions, config, _, key, _, before = crash_case
    with sessions.begin() as session:
        row = session.get(Translation, key)
        row.status, row.owner, row.attempts = "running", "legacy-worker-with-unknown-outcome", 1
        row.lease_until = "2000-01-01T00:00:00+00:00"
        assert "workflow_history" not in row.parts[0]
    await resume_without_model(sessions, config, key, force)
    with sessions() as session:
        assert_candidate_and_history_preserved(session.get(Translation, key), before)
