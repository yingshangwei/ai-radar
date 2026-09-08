import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from radar.schemas import DigestOutput, ReadingOutput
from radar.summary_contracts import (
    MAX_RESPONSE_CHARS,
    SummaryAuditOutput,
    SummaryUnit,
    audit_prompt,
    correction_prompt,
    digest_units,
    document_units,
    validate_audit,
    validate_corrections,
)


def sources():
    return [
        {"id": "a", "title": "AI tool support", "text": "The model supports 5 tools and 10 formats.",
         "author": "Researcher A", "url": "https://example.org/a", "published_at": "2026-09-08",
         "partial": True, "old_approval": "PRIVATE-OLD-APPROVAL", "review": {"approved": True},
         "text_zh": "PRIVATE-UNVERIFIED-TRANSLATION",
         "resources": [{"id": "resource-a", "title": "Technical report", "text": "The test used 5 tools.",
                        "author": "Report author", "partial": True, "summary_zh": "PRIVATE-OLD-SUMMARY"}]},
        {"id": "b", "title": "Another AI model", "text": "A different model supports 500 tools.",
         "author": "Researcher B"},
        {"id": "unused", "title": "Unused source", "text": "UNRELATED-SOURCE-TEXT"},
    ]


def digest(*, wrong=False):
    return DigestOutput.model_validate({
        "title": "人工智能工具进展", "overview": "两项模型研究更新了工具支持范围。",
        "stories": [
            {"title": "工具支持", "summary": f"研究者甲报告模型支持{500 if wrong else 5}种工具；仅取得部分正文。",
             "why_it_matters": "这可能扩展模型的适用场景。", "category": "模型", "source_ids": ["a"]},
            {"title": "另一模型支持范围", "summary": "研究者乙报告另一模型支持500种工具。",
             "why_it_matters": "这可能提供更多集成方式。", "category": "产品", "source_ids": ["b"]},
        ],
    })


def readings():
    return ReadingOutput.model_validate({"documents": [
        {"source_id": uid, "title_zh": "模型工具研究", "summary_zh": "模型报告了工具支持范围；仅取得部分正文。",
         "key_points_zh": ["作者描述了测试方法。", "尚不清楚其他配置下的效果。"],
         "why_it_matters_zh": "这可能帮助研究人员理解适用范围。"}
        for uid in ("a", "b")
    ]})


def audit_data(units):
    return {"audits": [{"unit_id": unit.unit_id, "approved": True, "issues": []} for unit in units]}


def encoded(data):
    return json.dumps(data, ensure_ascii=False)


def payload(prompt):
    return json.loads(prompt.split("\nUNTRUSTED_REVIEW_INPUT:\n", 1)[1])


def test_digest_units_keep_all_display_fields_and_limit_each_story_to_its_own_sources():
    original = digest()
    units = digest_units(original, sources())
    assert [(unit.unit_id, unit.kind, unit.source_ids) for unit in units] == [
        ("header", "header", ["a", "b"]), ("story:0", "story", ["a"]), ("story:1", "story", ["b"]),
    ]
    assert units[0].candidate == original.model_dump(include={"title", "overview"})
    assert [unit.candidate for unit in units[1:]] == [story.model_dump() for story in original.stories]


def test_document_units_keep_title_points_value_and_exact_document_citation():
    candidate = readings()
    units = document_units(candidate, sources()[:2])
    assert [unit.unit_id for unit in units] == ["document:a", "document:b"]
    assert [unit.candidate for unit in units] == [doc.model_dump() for doc in candidate.documents]


def test_numeric_contradiction_report_blocks_and_routes_only_failed_story_to_correction():
    # The protocol validates an independent auditor's diagnosis, rather than
    # pretending to infer entailment from schemas or number presence elsewhere.
    candidate = digest(wrong=True)
    units = digest_units(candidate, sources())
    data = audit_data(units)
    data["audits"][1].update(approved=False, issues=[{
        "field": "summary", "reason": "该来源只报告5种工具，候选误写成500种。", "source_ids": ["a"],
    }])

    result = validate_audit(encoded(data), units)
    corrected = payload(correction_prompt(units, sources(), result))

    assert not result.passed and not result.audits[1].passed
    assert [unit["unit_id"] for unit in corrected["units"]] == ["story:0"]
    assert corrected["unresolved_audits"] == [data["audits"][1]]
    assert [source["id"] for source in corrected["frozen_sources"]] == ["a"]
    assert corrected["units"][0]["candidate"] == candidate.stories[0].model_dump()


def test_normal_summary_can_omit_source_numbers_without_full_translation_equality_gate():
    units = digest_units(digest(), sources())
    assert "10" not in units[1].candidate["summary"]
    assert validate_audit(encoded(audit_data(units)), units).passed


@pytest.mark.parametrize("approved,issues,passed", [
    (True, [], True), (False, [], False),
    (True, [{"field": "summary", "reason": "尚有事实疑点。", "source_ids": ["a"]}], False),
    (False, [{"field": "summary", "reason": "尚有事实疑点。", "source_ids": ["a"]}], False),
])
def test_any_issue_or_explicit_rejection_blocks_even_if_approval_field_contradicts(approved, issues, passed):
    units = digest_units(digest(), sources())[1:2]
    result = validate_audit(encoded({"audits": [{"unit_id": units[0].unit_id,
                                                "approved": approved, "issues": issues}]}), units)
    assert result.passed is passed


@pytest.mark.parametrize("bad", ["true", "false", 1, 0, None])
def test_approval_is_strict_boolean_without_coercion(bad):
    units = digest_units(digest(), sources())
    data = audit_data(units)
    data["audits"][0]["approved"] = bad
    with pytest.raises(ValidationError):
        validate_audit(encoded(data), units)


@pytest.mark.parametrize("field", ["approved", "issues"])
def test_missing_required_approval_or_issues_cannot_default_to_pass(field):
    units = digest_units(digest(), sources())
    data = audit_data(units)
    data["audits"][0].pop(field)
    with pytest.raises(ValidationError):
        validate_audit(encoded(data), units)
    schema = SummaryAuditOutput.model_json_schema()["$defs"]["UnitAudit"]
    assert {"approved", "issues", "unit_id"}.issubset(schema["required"])
    assert "default" not in schema["properties"]["approved"]


@pytest.mark.parametrize("fault", ["unknown", "duplicate", "missing", "empty", "extra_field"])
def test_audit_unit_ids_must_be_complete_unique_and_schema_exact(fault):
    units = digest_units(digest(), sources())
    data = audit_data(units)
    if fault == "unknown":
        data["audits"][0]["unit_id"] = "not-requested"
    elif fault == "duplicate":
        data["audits"][1]["unit_id"] = data["audits"][0]["unit_id"]
    elif fault == "missing":
        data["audits"].pop()
    elif fault == "empty":
        data["audits"] = []
    else:
        data["audits"][0]["publish"] = True
    with pytest.raises(ValueError):
        validate_audit(encoded(data), units)


@pytest.mark.parametrize("ids", [[], ["b"], ["unknown"], ["a", "a"]])
def test_issue_requires_nonempty_real_evidence_within_that_unit(ids):
    units = digest_units(digest(), sources())
    data = audit_data(units)
    data["audits"][1].update(approved=False, issues=[{
        "field": "summary", "reason": "数字疑点。", "source_ids": ids,
    }])
    with pytest.raises(ValueError):
        validate_audit(encoded(data), units)


@pytest.mark.parametrize("kind,field", [("header", "summary"), ("story", "overview"), ("document", "category")])
def test_issue_fields_are_whitelisted_for_each_kind(kind, field):
    units = document_units(readings(), sources()[:2]) if kind == "document" else digest_units(digest(), sources())
    selected = next(unit for unit in units if unit.kind == kind)
    data = {"audits": [{"unit_id": selected.unit_id, "approved": False,
                        "issues": [{"field": field, "reason": "有问题。", "source_ids": selected.source_ids}]}]}
    with pytest.raises(ValueError):
        validate_audit(encoded(data), [selected])


def test_audit_prompt_contains_only_current_evidence_candidate_and_policy_without_old_approval():
    raw_sources = sources()
    units = digest_units(digest(), raw_sources)
    before = deepcopy((raw_sources, [unit.model_dump() for unit in units]))
    prompt = audit_prompt(units, raw_sources)
    body = payload(prompt)
    assert set(body) == {"policy", "frozen_sources", "units"}
    assert body["units"] == [unit.model_dump() for unit in units]
    assert [source["id"] for source in body["frozen_sources"]] == ["a", "b"]
    assert body["frozen_sources"][0]["text"] == raw_sources[0]["text"]
    assert body["frozen_sources"][0]["partial"]
    assert body["frozen_sources"][0]["resources"][0]["author"] == "Report author"
    assert all(marker not in prompt for marker in [
        "PRIVATE-OLD-APPROVAL", "PRIVATE-OLD-SUMMARY", "PRIVATE-UNVERIFIED-TRANSLATION", "UNRELATED-SOURCE-TEXT",
    ])
    assert "不调用工具" in prompt and "不联网" in prompt and "不要求逐句翻译或保留所有数字" in prompt
    assert (raw_sources, [unit.model_dump() for unit in units]) == before


def test_correction_of_rejected_without_detailed_issues_still_cannot_approve_itself():
    units = digest_units(digest(), sources())[1:2]
    data = audit_data(units)
    data["audits"][0]["approved"] = False
    audit = validate_audit(encoded(data), units)
    prompt = correction_prompt(units, sources(), audit)
    assert payload(prompt)["unresolved_audits"] == data["audits"]
    assert "不要返回 approved" in prompt
    valid = {"corrections": [{"unit_id": units[0].unit_id, "candidate": units[0].candidate}]}
    assert validate_corrections(encoded(valid), units) == units
    for location in ("correction", "candidate"):
        bad = deepcopy(valid)
        entry = bad["corrections"][0]
        (entry if location == "correction" else entry["candidate"])["approved"] = True
        with pytest.raises(ValueError):
            validate_corrections(encoded(bad), units)


def test_passed_units_are_not_sent_to_correction_and_corrections_cannot_change_scope():
    units = digest_units(digest(), sources())
    with pytest.raises(ValueError, match="No rejected"):
        correction_prompt(units, sources(), validate_audit(encoded(audit_data(units)), units))
    entry = {"unit_id": units[1].unit_id, "candidate": deepcopy(units[1].candidate)}
    entry["candidate"]["source_ids"] = ["b"]
    with pytest.raises(ValueError):
        validate_corrections(encoded({"corrections": [entry]}), [units[1]])


@pytest.mark.parametrize("fault", ["missing_source", "duplicate_source", "missing_candidate_field", "duplicate_document"])
def test_units_require_complete_candidates_and_actual_unique_source_packets(fault):
    raw_sources = sources()
    if fault == "missing_source":
        raw_sources = raw_sources[1:]
    elif fault == "duplicate_source":
        raw_sources.append(deepcopy(raw_sources[0]))
    elif fault == "missing_candidate_field":
        unit = digest_units(digest(), raw_sources)[1].model_dump()
        unit["candidate"].pop("why_it_matters")
        with pytest.raises(ValueError):
            SummaryUnit.model_validate(unit)
        return
    elif fault == "duplicate_document":
        candidate = readings()
        candidate.documents[1] = candidate.documents[0].model_copy(deep=True)
        with pytest.raises(ValueError):
            document_units(candidate, raw_sources[:2])
        return
    with pytest.raises(ValueError):
        digest_units(digest(), raw_sources)


def test_source_resource_needs_original_body_not_derived_summary_only():
    raw_sources = sources()
    raw_sources[0]["resources"][0].pop("text")
    with pytest.raises(ValueError):
        digest_units(digest(), raw_sources)


def test_unused_metadata_only_source_does_not_block_audit_or_correction():
    raw_sources = sources()[:2] + [{"id": "unused", "title": "Metadata only", "resources": "not-read"}]
    units = digest_units(digest(), raw_sources)
    reviewed = payload(audit_prompt(units, raw_sources))
    assert [source["id"] for source in reviewed["frozen_sources"]] == ["a", "b"]
    data = audit_data(units)
    data["audits"][1]["approved"] = False
    corrected = payload(correction_prompt(units, raw_sources, validate_audit(encoded(data), units)))
    assert [source["id"] for source in corrected["frozen_sources"]] == ["a"]
    assert "Metadata only" not in encoded(reviewed) + encoded(corrected)


@pytest.mark.parametrize("bad_id", [None, "", 1, True, "x" * 241])
def test_unused_source_id_still_must_be_valid_before_body_selection(bad_id):
    units = digest_units(digest(), sources())
    raw_sources = sources()[:2] + [{"id": bad_id}]
    with pytest.raises(ValueError, match="valid IDs"):
        audit_prompt(units, raw_sources)


def test_duplicate_unused_source_id_is_rejected_without_reading_its_body():
    units = digest_units(digest(), sources())
    with pytest.raises(ValueError, match="unique"):
        audit_prompt(units, sources()[:2] + [{"id": "unused"}, {"id": "unused"}])


def test_direct_resource_is_preserved_but_its_deeper_resources_never_enter_prompts():
    raw_sources = sources()
    direct = raw_sources[0]["resources"][0]
    direct["resources"] = [{
        "id": "deeper", "title": "DEEPER-PRIVATE-TITLE", "text": "DEEPER-PRIVATE-BODY",
        "resources": [{"id": "even-deeper", "text": "EVEN-DEEPER-PRIVATE-BODY"}],
    }]
    units = digest_units(digest(), raw_sources)
    data = audit_data(units)
    data["audits"][1]["approved"] = False
    prompts = [audit_prompt(units, raw_sources),
               correction_prompt(units, raw_sources, validate_audit(encoded(data), units))]
    for prompt in prompts:
        resource = payload(prompt)["frozen_sources"][0]["resources"][0]
        assert resource["id"] == "resource-a" and resource["text"] == direct["text"]
        assert resource["author"] == direct["author"] and resource["partial"]
        assert "resources" not in resource
        assert all(marker not in prompt for marker in [
            "DEEPER-PRIVATE-TITLE", "DEEPER-PRIVATE-BODY", "EVEN-DEEPER-PRIVATE-BODY",
        ])
    assert direct["resources"][0]["text"] == "DEEPER-PRIVATE-BODY"


def test_oversized_and_invalid_json_audits_fail_without_inventing_approval():
    units = digest_units(digest(), sources())
    for text in ("not-json", " " * (MAX_RESPONSE_CHARS + 1)):
        with pytest.raises(ValueError):
            validate_audit(text, units)
