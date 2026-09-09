"""Synthetic technical prose: preserve math and migrate approvals without resampling old failures."""
import json
from copy import deepcopy

import pytest

from radar import translation_workflow as workflow
from radar.config import TranslationConfig
from radar.db import database
from radar.math_text import formula_issues, math_spans, protect_html_math, restore_html_math, truncate_math
from radar.models import Translation
from radar.translation import (
    CONTEXT_POLICY,
    LEGACY_POLICY,
    RECHECK_POLICY,
    AuditedPart,
    TranslatedPart,
    TranslationService,
    candidate_fingerprint,
    context_recheck_needed,
    ensure_translation,
    part_audited,
    parts_for,
    quality_issues,
    review_policy,
)

TITLE = "A rate for adaptive optimization"
BODY = "arXiv preprint · Author abstract\n\nWe study the convergence rate of an adaptive optimization algorithm."
GOOD = {"title": "自适应优化的收敛速率", "body-0": "arXiv 预印本 · 作者摘要\n\n我们研究自适应优化算法的收敛速率。"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-only")
    engine, sessions = database(f"sqlite:///{tmp_path}/technical.db")
    config = TranslationConfig(enabled=True)
    yield sessions, config
    engine.dispose()


def seed(sessions, config, *, status="ready", unknown=False):
    with sessions.begin() as session:
        row = ensure_translation(session, TITLE, BODY, config)
        row.parts = [{**p, "zh": "旧的速率说明。", "draft": "旧的速率说明。", "ok": status == "ready",
                      "review": {"policy": LEGACY_POLICY, "round": 9, "approved": True, "issues": []},
                      "audit": {"policy": LEGACY_POLICY, "approved": status == "ready", "issues": [],
                                "fingerprint": candidate_fingerprint(p["source"], "旧的速率说明。")},
                      "workflow_history": ([workflow.event(p, LEGACY_POLICY, "reserved", call_id="unknown",
                                                          stage="audit", target="old")] if unknown else [])}
                     for p in row.parts]
        row.status, row.title_zh, row.text_zh = status, "旧的速率说明。", "旧的速率说明。"
        row.model, row.review_model = "synthetic-draft", "synthetic-review"
        return row.id, deepcopy(row.parts)


@pytest.mark.asyncio
async def test_context_upgrade_reuses_candidate_and_retains_history_once(store):
    sessions, config = store
    key, old = seed(sessions, config)
    with sessions.begin() as session:
        assert context_recheck_needed(session.get(Translation, key))
        row = ensure_translation(session, TITLE, BODY, config)
        assert row.status == "pending" and row.text_zh == "旧的速率说明。"
        assert all(p["audit"] == old[i]["audit"] for i, p in enumerate(row.parts))
    service, calls = TranslationService(sessions, config), []

    async def audit(parts):
        context = service.document_context()
        assert context["original_title"] == TITLE and context["original_sections"] == [BODY]
        assert context["candidate_sections"]
        calls.append("audit")
        return {p["id"]: AuditedPart(id=p["id"], approved=p["candidate"] == GOOD[p["id"]],
                                     issues=[] if p["candidate"] == GOOD[p["id"]] else ["术语所指的数学量不明确"])
                for p in parts}

    async def request(parts, *, review):
        assert review  # A policy upgrade does not discard a usable draft.
        calls.append("correction")
        return {p["id"]: TranslatedPart(id=p["id"], zh=GOOD[p["id"]], approved=True) for p in parts}

    service.audit, service.request = audit, request
    await service.translate_one(key, max_stage_calls=1)
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status != "ready" and service.can_progress(row)
    await service.translate_one(key)
    with sessions.begin() as session:
        row = session.get(Translation, key)
        assert row.status == "ready" and row.title_zh == GOOD["title"] and row.text_zh == GOOD["body-0"]
        assert all(part_audited(p, current=True) for p in row.parts)
        assert all(p["review_history"][0]["previous"]["audit"] == old[i]["audit"] for i, p in enumerate(row.parts))
        assert all(workflow.correction_rounds(p, RECHECK_POLICY) == 1 for p in row.parts)
        before = deepcopy(row.parts)
        assert ensure_translation(session, TITLE, BODY, config).parts == before
    await service.translate_one(key, force=True, recheck=True)
    assert calls == ["audit", "correction", "audit", "correction", "audit"]


@pytest.mark.asyncio
async def test_source_grounded_title_reuses_context_audited_body_and_omits_old_title(store):
    sessions, config = store
    key, _ = seed(sessions, config)
    with sessions.begin() as session:
        row = session.get(Translation, key)
        parts = deepcopy(row.parts)
        for part in parts:
            part['audit']['policy'] = CONTEXT_POLICY
        row.parts = parts
        body_before = deepcopy(parts[1])
        ensure_translation(session, TITLE, BODY, config)
        assert row.parts[0]['context_recheck_requested'] and row.parts[1] == body_before
    service, calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        part = payload['untrusted_parts'][0]
        assert len(payload['untrusted_parts']) == 1 and part['id'] == 'title'
        context = payload['untrusted_document_context']
        assert context['original_sections'] == [BODY]
        assert [c['id'] for c in context['candidate_sections']] == ['body-0']
        calls.append(stage)
        if stage == 'correction':
            assert 'draft' not in part and part['translation_mode'] == 'source_grounded_title'
            return json.dumps({'translations': [{'id': 'title', 'zh': GOOD['title'], 'approved': True, 'issues': []}]})
        assert stage == 'audit' and part['candidate'] == GOOD['title']
        return json.dumps({'audits': [{'id': 'title', 'approved': True, 'issues': []}]})

    service._completion = completion
    await service.translate_one(key)
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == 'ready' and row.title_zh == GOOD['title']
        assert row.parts[1] == body_before
        assert all(part_audited(p, current=True) for p in row.parts)
    await service.translate_one(key, force=True, recheck=True)
    assert calls == ['correction', 'audit']


@pytest.mark.parametrize("status,unknown", [("review_required", False), ("ready", True), ("error", True)])
@pytest.mark.asyncio
async def test_upgrade_never_resets_failed_old_workflow_or_unknown_call(store, status, unknown):
    sessions, config = store
    key, old = seed(sessions, config, status=status, unknown=unknown)
    with sessions.begin() as session:
        row = ensure_translation(session, TITLE, BODY, config)
        assert row.parts == old and not context_recheck_needed(row)
        assert review_policy(row) == LEGACY_POLICY
    service = TranslationService(sessions, config)

    async def forbidden(*args, **kwargs):
        pytest.fail("Old failures/unknown calls must not be resampled under a new policy")

    service.request = service.audit = forbidden
    await service.translate_one(key, force=True, recheck=True)
    with sessions() as session:
        assert session.get(Translation, key).parts == old


def test_formula_boundaries_currency_and_code_are_distinct():
    source = r"Value $x_2^{3}$ and \(\frac{1}{n}\), then $$a=b$$ and \[x+y\]."
    assert len(list(math_spans(source))) == 4
    assert not list(math_spans(r"Cost $5 and $10. Code `$x$` and ```$$x$$``` plus unmatched $open"))
    assert not formula_issues(source, "公式 " + source)
    assert formula_issues(source, source.replace("x_2", "x_3"))
    assert not quality_issues(r"The rate is $n^{-2}$.", r"收敛速率为 $n^{-2}$。")
    assert quality_issues("The price is $5 and $10.", "价格是 $5 和 $11。")


def test_chunking_never_splits_multiline_or_oversized_formula():
    formula = "$$" + "x_i +\n" * 1000 + "y^2$$"
    parts = parts_for("A formula", "Context. " * 440 + formula + " Last sentence.")
    assert [value for p in parts for _, _, value in math_spans(p["source"])] == [formula]
    formula_only = parts_for(formula, formula)
    assert formula_only[0]["zh"] == formula and part_audited(formula_only[0], current=True)


@pytest.mark.asyncio
async def test_model_receives_protected_math_and_source_context(store):
    sessions, config = store
    service = TranslationService(sessions, config)
    source = r"The rate is $\frac{1}{n^2}$ and \(x_t\)."
    seen = []

    async def completion(payload, **kwargs):
        seen.append(deepcopy(payload))
        assert "$\\frac" not in payload["untrusted_parts"][0]["source"]
        assert payload["untrusted_document_context"]["original_title"] == TITLE
        return json.dumps({"translations": [{"id": "body-0", "zh": "收敛速率为 ⟪原文公式-0⟫ 和 ⟪原文公式-1⟫。",
                                              "approved": True, "issues": []}]})

    service._completion = completion
    token = service._document_context.set({"original_title": TITLE, "original_sections": [source]})
    try:
        result = await service.request([{"id": "body-0", "source": source}], review=False)
    finally:
        service._document_context.reset(token)
    assert result["body-0"].zh == r"收敛速率为 $\frac{1}{n^2}$ 和 \(x_t\)。"
    assert len(seen) == 1


def test_publisher_tex_survives_html_extraction_without_duplicate_visual_math():
    from radar.page_parser import parse_page

    formula = r"\frac{1}{n_2^3}"
    source = ("<html><head><title>Optimization study</title></head><body><article><h1>Optimization study</h1><p>"
              + "The authors report a convergence result for an adaptive algorithm. " * 5
              + '<math display="block" alttext="' + formula + '"><mfrac><mn>1</mn><mn>123</mn></mfrac></math>'
              + '<span class="katex"><span class="katex-mathml"><math><semantics><mi>x</mi>'
                '<annotation encoding="application/x-tex">x_t^2</annotation></semantics></math></span>'
                '<span class="katex-html">DOUBLED-VISUAL-TEXT</span></span>'
              + r' An explicit formula $y_t^2$ and a raw equation \[a=b\].'
              + '</p></article></body></html>').encode()
    page = parse_page(source, 'text/html', 'https://example.org/paper')
    assert r'\[' + formula + r'\]' in page['text']
    assert r'\(x_t^2\)' in page['text'] and 'DOUBLED-VISUAL-TEXT' not in page['text']
    assert '$y_t^2$' in page['text'] and r'\[a=b\]' in page['text']
    assert 'RADARMATH' not in page['text']
    html, protected = protect_html_math(source)
    assert len(protected) == 4 and formula in restore_html_math(html, protected)
    assert truncate_math('before ' + '$$' + 'x'*100 + '$$ after', 50) == 'before '
