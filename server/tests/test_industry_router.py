"""Exercise industry schemas through local fake Codex subprocesses and receipts."""

import json
import sys

import pytest
from openai.lib._pydantic import to_strict_json_schema

from radar import model_router, usage
from radar.config import ProviderConfig
from radar.industry_contracts import IndustryAudit, IndustryReport, prompt
from radar.providers import CLIProvider

FAKE = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
request = sys.stdin.read()
schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())
def check(node):
    if isinstance(node, dict):
        assert 'default' not in node
        if node.get('type') == 'object':
            assert node.get('additionalProperties') is False
            assert set(node['required']) == set(node['properties'])
        for child in node.values(): check(child)
    elif isinstance(node, list):
        for child in node: check(child)
check(schema)
mode = os.environ.get('INDUSTRY_ROUTER_FIXTURE_MODE', 'resolved')
audit = {'approved': True, 'citations_supported': True, 'numbers_and_dates_correct': True,
         'uncertainty_preserved': True, 'no_invented_market_data': True, 'issues': []}
claim = {'text_zh': '仅凭一份披露仍需进一步验证。', 'source_ids': ['fixture-evidence']}
report = {'state': 'insufficient_evidence', 'summary_zh': '来源披露收入变化，行业方向尚待验证。',
          'supporting': [claim], 'opposing': [], 'investment_implications': [],
          'watch_items': [claim], 'unknowns': ['市场预期差未知。'], 'horizon': '未来两季度'}
value = audit if 'Audit' in schema['title'] else report
if schema['title'].startswith('Confirmed'):
    gate = {'decision': 'resolved', 'reason': 'none', 'evidence_quotes': [], 'question_zh': ''}
    if mode in ('escalate', 'bad_quote'):
        gate = {'decision': 'needs_adjudication', 'reason': 'complex_inference',
                'evidence_quotes': ['Revenue grew but margin fell.' if mode == 'escalate'
                                    else 'Invented text absent from evidence.'],
                'question_zh': '收入与毛利率方向不同，能否确定经营状态？'}
    if mode == 'missing':
        gate = {'decision': 'insufficient_evidence', 'reason': 'missing_evidence',
                'evidence_quotes': [], 'question_zh': '缺少必要来源'}
        value = None
    value = {'gate': gate, 'result': value}
text = json.dumps(value, ensure_ascii=False)
if '--output-last-message' in args:
    Path(args[args.index('--output-last-message') + 1]).write_text(text)
for event in [
    {'type': 'thread.started', 'thread_id': '0199a213-81c0-7800-8aa1-bbab2a035a53'},
    {'type': 'turn.started'},
    {'type': 'item.completed', 'item': {'type': 'agent_message', 'id': 'final', 'text': text}},
    {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 5}},
]:
    print(json.dumps(event), flush=True)
'''


@pytest.fixture
def routed(tmp_path, monkeypatch):
    policy = tmp_path / "routing.toml"
    policy.write_text('enabled=true\nstate_directory=' + json.dumps(str(tmp_path / "receipts")))
    executable = tmp_path / "fake.py"
    executable.write_text(FAKE)
    monkeypatch.setenv("RADAR_MODEL_ROUTING_CONFIG", str(policy))
    monkeypatch.setenv("RADAR_USAGE_DATABASE_PATH", str(tmp_path / "usage.db"))
    monkeypatch.delenv("INDUSTRY_ROUTER_FIXTURE_MODE", raising=False)
    model_router._load.cache_clear()
    provider = ProviderConfig(kind="codex", command=[sys.executable, str(executable)], timeout_seconds=30,
                              env_allowlist=["INDUSTRY_ROUTER_FIXTURE_MODE"])
    yield provider
    model_router._load.cache_clear()


def task(stage):
    candidate = None if stage == "generation" else {"summary_zh": "待审核的候选"}
    correction = {"issues": ["保留数字口径"]} if stage == "correction" else None
    return prompt({"id": "infrastructure"},
                  [{"id": "fixture-evidence", "text": "Revenue grew but margin fell."}],
                  "2026-09-13T04:00:00+00:00", candidate=candidate, audit=correction)


@pytest.mark.parametrize("schema", [IndustryReport, IndustryAudit, model_router.confirmation_schema(IndustryAudit)])
def test_direct_cli_schema_is_already_sdk_strict(schema):
    assert schema.model_json_schema() == to_strict_json_schema(schema)
    if schema is IndustryAudit:
        assert "passed" not in schema.model_json_schema()["properties"]


@pytest.mark.parametrize("stage", ["generation", "correction"])
async def test_industry_generation_and_correction_route_one_sol_call(routed, stage):
    text, schema = task(stage)
    with usage.scope("industry_research", stage):
        value = await CLIProvider(routed).complete(text, schema)
    assert IndustryReport.model_validate_json(value).state == "insufficient_evidence"
    rows = usage.store().report("all")["recent"]
    assert len(rows) == 1
    assert rows[0]["feature"] == "industry_research"
    assert rows[0]["stage"] == f"{stage}_standard_medium"
    assert rows[0]["model"] == "gpt-5.6-sol"


@pytest.mark.parametrize("mode,count", [("resolved", 1), ("escalate", 2)])
async def test_industry_audit_stops_at_low_or_one_medium_and_reuses_receipts(routed, monkeypatch, mode, count):
    monkeypatch.setenv("INDUSTRY_ROUTER_FIXTURE_MODE", mode)
    text, schema = task("audit")
    assert schema is IndustryAudit
    with usage.scope("industry_research", "audit"):
        first = await CLIProvider(routed).complete(text, schema)
        second = await CLIProvider(routed).complete(text, schema)
    assert first == second and IndustryAudit.model_validate_json(first).passed
    rows = usage.store().report("all")["recent"]
    assert len(rows) == count
    assert all(row["feature"] == "industry_research" and row["model"] == "gpt-6-astra" for row in rows)
    assert {row["stage"] for row in rows} == ({"audit_confirmation_low"} if count == 1
        else {"audit_confirmation_low", "audit_adjudication_medium"})


@pytest.mark.parametrize("mode", ["missing", "bad_quote"])
async def test_industry_audit_missing_evidence_or_unquoted_conflict_cannot_escalate(routed, monkeypatch, mode):
    monkeypatch.setenv("INDUSTRY_ROUTER_FIXTURE_MODE", mode)
    text, schema = task("audit")
    with usage.scope("industry_research", "audit"), pytest.raises(ValueError):
        await CLIProvider(routed).complete(text, schema)
    rows = usage.store().report("all")["recent"]
    assert len(rows) == 1 and rows[0]["stage"] == "audit_confirmation_low"
