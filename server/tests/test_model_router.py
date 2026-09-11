"""Real local subprocess and SQLite tests; no network, auth or model calls."""

import asyncio
import json
import sys
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from radar import codex_sessions, model_router, usage
from radar.config import ProviderConfig, RadarConfig
from radar.db import database
from radar.discovery import DiscoveryService, queue_candidate
from radar.discovery_sessions import DiscoverySessions
from radar.models import AgentBatch, AgentSession, DiscoveryCandidate
from radar.providers import CLIProvider
from radar.schemas import IncomingArticle


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool


FAKE = r'''
import json,sys,os
from pathlib import Path
args=sys.argv[1:];prompt=sys.stdin.read()
Path('invoked.json').write_text(json.dumps(args))
schema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())
mode=os.environ.get('RADAR_FAKE_MODE','resolved')
thread=args[-2] if 'resume' in args else '0199a213-81c0-7800-8aa1-bbab2a035a53'
def emit(data): print(json.dumps(data),flush=True)
emit({'type':'thread.started','thread_id':thread});emit({'type':'turn.started'})
if mode=='transport': sys.exit(1)
result={'approved':True};quote='The reported result is conditional.'
if '\nCURRENT_BATCH:\n' in prompt:
 data=json.JSONDecoder().raw_decode(prompt.split('\nCURRENT_BATCH:\n')[1])[0]
 quote=data['candidates'][0]['text'][:50]
 result={'batch_id':data['batch_id'],'decisions':[{'candidate_id':c['candidate_id'],'is_ai_relevant':True,'novelty':20,'specificity':20,'potential_impact':20,'confidence':50,'should_surface':False,'reason_zh':'现有证据不足以支持新增关注','uncertainty_zh':'需要更多后续观察','evidence_quotes':[],'entities':[],'named_entities':[]} for c in data['candidates']]}
if schema['title'].startswith('Confirmed'):
 gate={'decision':'resolved','reason':'none','question_zh':'','evidence_quotes':[]}
 if mode in ('escalate','bad_quote'):
  gate={'decision':'needs_adjudication','reason':'complex_inference','question_zh':'两个条件下的结论是否具有相同适用范围？','evidence_quotes':[quote if mode=='escalate' else 'This does not occur in the evidence.']}
 if mode=='missing': gate={'decision':'insufficient_evidence','reason':'missing_evidence','question_zh':'缺少原文','evidence_quotes':[]}
 result={'gate':gate,'result':result}
text='not json' if mode=='invalid' else json.dumps(result)
if '--output-last-message' in args: Path(args[args.index('--output-last-message')+1]).write_text(text)
emit({'type':'item.completed','item':{'type':'agent_message','id':'final','text':text}})
emit({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}})
'''


@pytest.fixture
def routed(tmp_path, monkeypatch):
    file = tmp_path / "routing.toml"
    file.write_text('enabled=true\nstate_directory=' + json.dumps(str(tmp_path / "requests")))
    script = tmp_path / "fake.py"
    script.write_text(FAKE)
    monkeypatch.setenv("RADAR_MODEL_ROUTING_CONFIG", str(file))
    monkeypatch.setenv("RADAR_USAGE_DATABASE_PATH", str(tmp_path / "usage.db"))
    monkeypatch.delenv("RADAR_FAKE_MODE", raising=False)
    model_router._load.cache_clear()
    provider = ProviderConfig(command=[sys.executable, str(script)], timeout_seconds=30,
        env_allowlist=["RADAR_FAKE_MODE"])
    yield provider, tmp_path
    model_router._load.cache_clear()


PROMPT = 'Original review task\nUNTRUSTED_DATA:\n{"text":"The reported result is conditional."}'


async def test_ordinary_work_only_uses_sol_medium(routed):
    provider, _ = routed
    with usage.scope("digest"):
        assert json.loads(await CLIProvider(provider).complete(PROMPT, Answer))["approved"]
    rows = usage.store().report("all")["recent"]
    assert len(rows) == 1
    assert rows[0]["model"] == "gpt-5.6-sol" and rows[0]["stage"] == "generation_standard_medium"
    assert 'model_reasoning_effort="medium"' in model_router.provider_for(provider, "standard").command


async def test_confirmed_work_stops_at_low_and_recovers_same_receipt_without_charge(routed):
    provider, root = routed
    with usage.scope("digest", "audit"):
        first = await CLIProvider(provider).complete(PROMPT, Answer)
        second = await CLIProvider(provider).complete(PROMPT, Answer)
    assert first == second
    rows = usage.store().report("all")["recent"]
    assert len(rows) == 1 and rows[0]["stage"] == "audit_confirmation_low"
    args = json.loads(next((root / "requests").rglob("invoked.json")).read_text())
    assert args[args.index("--model") + 1] == "gpt-6-astra"
    assert 'model_reasoning_effort="low"' in args and "--ignore-user-config" in args


async def test_concurrent_confirmation_waits_for_the_same_call(routed):
    provider, _ = routed
    with usage.scope("web_reading", "audit"):
        values = await asyncio.gather(*(CLIProvider(provider).complete(PROMPT, Answer) for _ in range(3)))
    assert len(set(values)) == 1
    assert usage.store().report("all")["totals"]["calls"] == 1


async def test_explicit_evidenced_disagreement_escalates_once_and_keeps_usage_separate(routed, monkeypatch):
    provider, _ = routed
    monkeypatch.setenv("RADAR_FAKE_MODE", "escalate")
    with usage.scope("technical_translation", "audit"):
        result = await CLIProvider(provider).complete(PROMPT, Answer)
        again = await CLIProvider(provider).complete(PROMPT, Answer)
    assert result == again and json.loads(result)["approved"]
    rows = usage.store().report("all")["recent"]
    assert len(rows) == 2
    assert {r["stage"] for r in rows} == {"audit_confirmation_low", "audit_adjudication_medium"}
    assert sum(r["total_tokens"] for r in rows) == 30


@pytest.mark.parametrize("mode", ["missing", "invalid", "bad_quote", "transport"])
async def test_missing_data_bad_output_and_transport_never_escalate(routed, monkeypatch, mode):
    provider, _ = routed
    monkeypatch.setenv("RADAR_FAKE_MODE", mode)
    with usage.scope("web_reading", "audit"), pytest.raises((ValueError, codex_sessions.CodexSessionError)):
        await CLIProvider(provider).complete(PROMPT, Answer)
    rows = usage.store().report("all")["recent"]
    assert len(rows) == 1 and rows[0]["stage"] == "audit_confirmation_low"


def test_non_codex_and_disabled_policy_preserve_existing_provider_and_business_fingerprints(routed, monkeypatch):
    provider, _ = routed
    before = provider.model_dump_json()
    assert not model_router.active(ProviderConfig(kind="openai_chat", model="qwen-plus"))
    model_router.provider_for(provider, "standard")
    assert provider.model_dump_json() == before
    monkeypatch.delenv("RADAR_MODEL_ROUTING_CONFIG")
    assert not model_router.active(provider)


def test_usage_allocation_distinguishes_conditional_medium_and_keeps_bailian(routed):
    provider, _ = routed
    config = RadarConfig(provider=provider, translation={"enabled": True,
        "base_url": "https://example.aliyuncs.com/v1", "model": "qwen-flash", "review_model": "qwen-plus",
        "technical_review_provider": provider})
    rows = usage.allocation(config)
    assert any(r["model"] == "qwen-flash" and r["stage"] == "draft" for r in rows)
    assert any(r["model"] == "gpt-5.6-sol" and r["stage"] == "generation_standard_medium" for r in rows)
    assert any(r["model"] == "gpt-6-astra" and r["stage"] == "audit_adjudication_medium" for r in rows)


def discovery_fixture(routed):
    provider, root = routed
    engine, sessions = database(f"sqlite:///{root}/discovery.db")
    config = RadarConfig(provider=provider, discovery={"enabled": True, "session_reuse": True,
        "session_directory": str(root / "discovery-sessions"), "batch_size": 1})
    service = DiscoveryService(sessions, config, lambda *_: None)

    def enqueue(index):
        article = IncomingArticle(platform="x", external_id=str(index),
            url=f"https://x.com/example/status/{index}", title="AI benchmark release", author="Research",
            text="The AI benchmark reports conditional results and requires careful technical interpretation.",
            published_at=datetime.now(UTC), metrics={"like_count": 1})
        with sessions.begin() as session:
            return queue_candidate(session, article, config).id

    return engine, sessions, service, enqueue


async def test_discovery_low_session_reused_for_multiple_batches(routed):
    engine, sessions, service, enqueue = discovery_fixture(routed)
    try:
        enqueue(1)
        assert (await service.pending())["judged"] == 1
        enqueue(2)
        assert (await service.pending())["judged"] == 1
        with sessions() as db:
            conversations = list(db.scalars(select(AgentSession)))
            assert len(conversations) == 1 and conversations[0].turns == 2
            assert all(b.status == "completed" for b in db.scalars(select(AgentBatch)))
        invocations = list((routed[1] / "discovery-sessions").rglob("invoked.json"))
        assert len(invocations) == 2
        assert sum("resume" in json.loads(p.read_text()) for p in invocations) == 1
    finally:
        engine.dispose()


async def test_discovery_crash_after_low_resumes_adjudication_without_repeating_low(routed, monkeypatch):
    engine, sessions, service, enqueue = discovery_fixture(routed)
    monkeypatch.setenv("RADAR_FAKE_MODE", "escalate")
    try:
        key = enqueue(1)
        controller = DiscoverySessions(service)
        with controller._exclusive() as fd:
            batch, thread = controller._reserve(1)
            with usage.scope("discovery_foresight", "generation_confirmation_low"):
                await codex_sessions.run(controller.provider, batch.workdir, batch.prompt, controller.schema,
                    session_id=thread, lock_fd=fd, on_thread=lambda t: controller._bind_thread(batch.id, t))
        # Simulate a process exit after the low receipt and before any medium request or DB acceptance.
        service.recover()
        with sessions() as db:
            assert db.get(DiscoveryCandidate, key).status == "reserved"
        await service.pending(limit=0)
        assert usage.store().report("all")["totals"]["calls"] == 1
        await service.pending()
        await service.pending()
        with sessions() as db:
            assert db.get(AgentBatch, batch.id).status == "completed"
        rows = usage.store().report("all")["recent"]
        assert len(rows) == 2
        assert {r["stage"] for r in rows} == {"generation_confirmation_low", "generation_adjudication_medium"}
    finally:
        engine.dispose()
