"""Saved model approvals survive corrected deterministic checks without another model call."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from radar.config import TranslationConfig
from radar.db import database
from radar.models import (
    Article,
    ArticleDocument,
    ArticleTranslation,
    Translation,
    TranslationAccountState,
    WebDocument,
)
from radar.translation import (
    RECHECK_POLICY,
    TranslationService,
    account_scope,
    cache_key,
    candidate_fingerprint,
    ensure_translation,
    part_audited,
    quality_issues,
    translation_status,
)

SOURCE = "The AI success rate is 20 percent."
CANDIDATE = "人工智能成功率为20%。"
OLD_MACHINE_ISSUE = "百分比或货币标记不一致"
SEMANTIC_ISSUE = "候选把来源的可能性改为确定结论。"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-only")
    engine, sessions = database(f"sqlite:///{tmp_path}/machine-revalidation.db")

    async def forbidden(*args, **kwargs):
        pytest.fail("Machine revalidation must not invoke translation or audit models")

    monkeypatch.setattr(TranslationService, "_completion", forbidden)
    monkeypatch.setattr(TranslationService, "request", forbidden)
    monkeypatch.setattr(TranslationService, "audit", forbidden)
    yield sessions
    engine.dispose()


def snapshot(sessions, key):
    with sessions() as session:
        row = session.get(Translation, key)
        return deepcopy({column.name: getattr(row, column.name) for column in row.__table__.columns})


def seed(sessions, config, *, text=SOURCE, candidate=CANDIDATE, title=None, title_zh=None):
    title = text if title is None else title
    with sessions.begin() as session:
        row = ensure_translation(session, title, text, config)
        parts = deepcopy(row.parts)
        for part in parts:
            chinese = title_zh if part["id"] == "title" else candidate
            assert quality_issues(part["source"], chinese) == []
            fingerprint = candidate_fingerprint(part["source"], chinese)
            review = {
                "policy": RECHECK_POLICY, "model": config.review_model,
                "fingerprint": fingerprint, "approved": True, "issues": [], "round": 2,
                "at": "2026-09-01T00:00:00+00:00",
            }
            audit = {
                "policy": RECHECK_POLICY, "model": config.audit_model or config.review_model,
                "fingerprint": fingerprint, "approved": True, "issues": [],
                "correction_issues": [], "machine_issues": [OLD_MACHINE_ISSUE],
                "at": "2026-09-01T00:00:01+00:00",
            }
            part.update(
                zh=chinese, draft=chinese, initial_draft=chinese, ok=False,
                correction_required=True, issues=[OLD_MACHINE_ISSUE], review=review, audit=audit,
                quality_history=[{"kind": "correction", **review}, {"kind": "audit", **audit}],
            )
        row.parts, row.status = parts, "review_required"
        row.attempts = config.max_attempts
        row.retry_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        row.issues = [OLD_MACHINE_ISSUE]
        row.model, row.review_model = "saved-draft-model", "saved-correction-model"
        return row.id


def bind_article(sessions, key, uid="main", *, age=0):
    with sessions.begin() as session:
        row = session.get(Translation, key)
        session.add(Article(
            id=uid, platform="x", source_id="x", external_id=uid,
            url=f"https://x.com/example/status/{uid}", canonical_url=f"https://x.com/example/status/{uid}",
            title=row.original_title, text=row.original_text, author="Synthetic",
            published_at=(datetime.now(UTC) - timedelta(hours=age)).isoformat(),
        ))
        session.flush()
        session.add(ArticleTranslation(article_id=uid, translation_id=key))
    return uid


def bind_document(sessions, key, uid, article_id=None, *, current_text=None):
    with sessions.begin() as session:
        row = session.get(Translation, key)
        session.add(WebDocument(
            id=uid, url=f"https://example.org/{uid}", title=row.original_title,
            text=row.original_text if current_text is None else current_text, status="fetched",
        ))
        session.flush()
        if article_id is not None:
            session.add(ArticleDocument(article_id=article_id, document_id=uid, relation="link"))


def mutate(sessions, key, change):
    with sessions.begin() as session:
        row = session.get(Translation, key)
        parts = deepcopy(row.parts)
        change(parts[0])
        row.parts = parts


def assert_evidence_preserved(before, after):
    for name in ("original_title", "original_text", "revision", "attempts", "model", "review_model"):
        assert after[name] == before[name]
    for old, new in zip(before["parts"], after["parts"], strict=True):
        for name in ("source", "draft", "zh", "initial_draft", "review", "audit", "quality_history"):
            assert new[name] == old[name]


def test_current_exact_approvals_recover_exhausted_row_without_models_and_are_idempotent(store):
    config = TranslationConfig(enabled=True)
    key = seed(store, config)
    before = snapshot(store, key)
    service = TranslationService(store, config)

    assert service.reconcile_machine_checks([key, key]) == {key}

    after = snapshot(store, key)
    assert after["status"] == "ready" and after["text_zh"] == CANDIDATE
    assert after["title_zh"] == CANDIDATE and after["issues"] == []
    assert not after["owner"] and not after["lease_until"]
    assert_evidence_preserved(before, after)
    part = after["parts"][0]
    assert part_audited(part) and part["issues"] == [] and not part["correction_required"]
    assert len(part["machine_history"]) == 1
    assert service.reconcile_machine_checks([key]) == set()
    assert snapshot(store, key) == after


@pytest.mark.parametrize("fault", [
    "audit_rejected", "review_rejected", "audit_issue", "review_issue", "correction_issue",
    "audit_policy", "review_policy", "audit_fingerprint", "review_fingerprint",
    "missing_audit", "missing_review", "approved_string", "unexplained_issue",
    "missing_machine_issues", "editorial", "recheck_pending", "different_draft", "changed_source",
])
def test_no_shortcut_for_semantics_untrusted_approval_or_unmatched_provenance(store, fault):
    config = TranslationConfig(enabled=True)
    key = seed(store, config)

    def change(part):
        if fault.endswith("_rejected"):
            part[fault.removesuffix("_rejected")]["approved"] = False
        elif fault in {"audit_issue", "review_issue"}:
            part[fault.removesuffix("_issue")]["issues"] = [SEMANTIC_ISSUE]
        elif fault == "correction_issue":
            part["audit"]["correction_issues"] = [SEMANTIC_ISSUE]
        elif fault.endswith("_policy"):
            part[fault.removesuffix("_policy")]["policy"] = "prior-audit-policy"
        elif fault.endswith("_fingerprint"):
            part[fault.removesuffix("_fingerprint")]["fingerprint"] = "different-source-candidate"
        elif fault.startswith("missing_"):
            if fault == "missing_machine_issues":
                part["audit"].pop("machine_issues")
            else:
                part.pop(fault.removeprefix("missing_"))
        elif fault == "approved_string":
            part["audit"]["approved"] = "true"
        elif fault == "unexplained_issue":
            part["issues"].append(SEMANTIC_ISSUE)
        elif fault == "editorial":
            part["editorial_previous_zh"] = part["zh"]
        elif fault == "recheck_pending":
            part["recheck_pending"] = True
        elif fault == "different_draft":
            part["draft"] += "候选内容已变化。"
        elif fault == "changed_source":
            # Matching inner hashes do not excuse a part that no longer derives
            # from the immutable source row.
            part["source"] = "A different AI success rate is 20 percent."
            for name in ("review", "audit"):
                part[name]["fingerprint"] = candidate_fingerprint(part["source"], part["zh"])

    mutate(store, key, change)
    before = snapshot(store, key)
    assert TranslationService(store, config).reconcile_machine_checks([key]) == set()
    assert snapshot(store, key) == before


@pytest.mark.parametrize("fault", ["active_lease", "editorial_model", "old_cache", "missing_part", "extra_part"])
def test_row_scope_and_complete_original_parts_are_required(store, fault):
    config = TranslationConfig(enabled=True)
    key = seed(store, config)
    with store.begin() as session:
        row = session.get(Translation, key)
        if fault == "active_lease":
            row.owner = "another-worker"
            row.lease_until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        elif fault == "editorial_model":
            row.review_model = "saved-model-editorial"
        elif fault == "missing_part":
            row.parts = []
        elif fault == "extra_part":
            row.parts = row.parts + [{**deepcopy(row.parts[0]), "id": "body-extra"}]
    if fault == "old_cache":
        config = config.model_copy(update={"revision": "different-translation-revision"})
    before = snapshot(store, key)
    assert TranslationService(store, config).reconcile_machine_checks([key]) == set()
    assert snapshot(store, key) == before


def test_new_real_machine_failure_is_not_removed_by_old_model_approvals(store):
    config = TranslationConfig(enabled=True)
    key = seed(store, config)

    def change(part):
        part["draft"] = part["zh"] = "人工智能成功率为200%。"
        for name in ("review", "audit"):
            part[name]["fingerprint"] = candidate_fingerprint(part["source"], part["zh"])

    mutate(store, key, change)
    before = snapshot(store, key)
    assert quality_issues(before["parts"][0]["source"], before["parts"][0]["zh"])
    assert TranslationService(store, config).reconcile_machine_checks([key]) == set()
    assert snapshot(store, key) == before


def test_partial_recovery_preserves_other_part_semantic_rejection_and_all_history(store):
    config = TranslationConfig(enabled=True)
    key = seed(store, config, title="AI success rate", title_zh="人工智能成功率")

    def reject_title(part):
        part["audit"]["approved"] = False
        part["audit"]["issues"] = [SEMANTIC_ISSUE]
        part["issues"].append(SEMANTIC_ISSUE)

    mutate(store, key, reject_title)
    before = snapshot(store, key)
    service = TranslationService(store, config)
    assert service.reconcile_machine_checks([key]) == {key}
    after = snapshot(store, key)
    assert after["status"] == "review_required" and not after["text_zh"]
    assert after["parts"][0] == before["parts"][0]
    assert part_audited(after["parts"][1])
    assert SEMANTIC_ISSUE in after["issues"]
    assert_evidence_preserved(before, after)
    assert service.reconcile_machine_checks([key]) == set()
    assert snapshot(store, key) == after


@pytest.mark.asyncio
async def test_forced_pending_does_not_resample_semantically_rejected_partial_recovery(store):
    config = TranslationConfig(enabled=True)
    key = seed(store, config, title="AI success rate", title_zh="人工智能成功率")
    bind_article(store, key)

    def reject_title(part):
        part["audit"]["approved"] = False
        part["audit"]["issues"] = [SEMANTIC_ISSUE]
        part["issues"].append(SEMANTIC_ISSUE)

    mutate(store, key, reject_title)
    before = snapshot(store, key)

    result = await TranslationService(store, config).pending(force=True)

    after = snapshot(store, key)
    assert result["counts"] == {"review_required": 1}
    assert after["parts"][0] == before["parts"][0]
    assert part_audited(after["parts"][1])
    assert_evidence_preserved(before, after)


def test_previously_ok_sibling_with_new_machine_error_blocks_entire_row_reconciliation(store):
    config = TranslationConfig(enabled=True)
    key = seed(store, config, title="AI success rate", title_zh="人工智能成功率")

    def damage_title(part):
        part.update(ok=True, correction_required=False, issues=[], zh="人工智能第2成功率", draft="人工智能第2成功率")
        for name in ("review", "audit"):
            part[name]["fingerprint"] = candidate_fingerprint(part["source"], part["zh"])
        part["audit"]["machine_issues"] = []
        assert part_audited(part) and quality_issues(part["source"], part["zh"])

    mutate(store, key, damage_title)
    before = snapshot(store, key)

    assert TranslationService(store, config).reconcile_machine_checks([key]) == set()

    assert snapshot(store, key) == before


@pytest.mark.asyncio
async def test_pending_recovers_bound_main_and_web_but_not_orphan_or_stale_cache(store):
    config = TranslationConfig(enabled=True)
    main = seed(store, config)
    article = bind_article(store, main)
    web = seed(store, config, text="AI precision is 20 percent.", candidate="人工智能精度为20%。")
    bind_document(store, web, "bound", article)
    bind_document(store, web, "shared", article)
    orphan = seed(store, config, text="AI recall is 20 percent.", candidate="人工智能召回率为20%。")
    bind_document(store, orphan, "orphan")
    stale = seed(store, config, text="AI accuracy is 20 percent.", candidate="人工智能准确率为20%。")
    bind_document(store, stale, "changed", article, current_text=SOURCE)
    # Match the replacement page's title to an already current cache. The old
    # cache must remain entirely untouched, with no model work for a new row.
    with store.begin() as session:
        session.get(WebDocument, "changed").title = SOURCE
    protected = {key: snapshot(store, key) for key in (orphan, stale)}

    result = await TranslationService(store, config).pending()

    assert result["counts"] == {"ready": 1}
    assert result["resource_counts"] == {"ready": 3}
    assert snapshot(store, web)["status"] == "ready"
    assert len(snapshot(store, web)["parts"][0]["machine_history"]) == 1
    assert all(snapshot(store, key) == before for key, before in protected.items())


@pytest.mark.asyncio
async def test_pending_revalidation_limit_keeps_main_priority_and_can_finish_next_run(store):
    config = TranslationConfig(enabled=True, max_documents=1)
    main = seed(store, config)
    article = bind_article(store, main)
    web = seed(store, config, text="AI precision is 20 percent.", candidate="人工智能精度为20%。")
    bind_document(store, web, "bound", article)
    web_before = snapshot(store, web)
    service = TranslationService(store, config)

    first = await service.pending()
    assert first["counts"] == {"ready": 1}
    assert first["resource_counts"] == {"review_required": 1}
    assert snapshot(store, web) == web_before
    second = await service.pending()
    assert second["resource_counts"] == {"ready": 1}


@pytest.mark.asyncio
async def test_evidence_reconciles_saved_approvals_before_attempts_filter_and_reuses_result(store):
    config = TranslationConfig(enabled=True)
    key = seed(store, config)
    before = snapshot(store, key)
    evidence = {"id": "synthetic", "title": SOURCE, "text": SOURCE}
    service = TranslationService(store, config)

    result = await service.evidence([evidence])

    assert result == [{**evidence, "title_zh": CANDIDATE, "text_zh": CANDIDATE}]
    after = snapshot(store, key)
    assert_evidence_preserved(before, after)
    assert await service.evidence([evidence]) == result
    assert snapshot(store, key) == after


@pytest.mark.asyncio
async def test_zero_model_revalidation_needs_no_key_and_does_not_clear_balance_alert(store, monkeypatch):
    config = TranslationConfig(enabled=True)
    key = seed(store, config)
    bind_article(store, key)
    with store.begin() as session:
        session.add(TranslationAccountState(
            id=account_scope(config), code="insufficient_balance", observed_at="2026-09-01T01:00:00+00:00",
        ))
    monkeypatch.delenv("DEEPSEEK_API_KEY")

    result = await TranslationService(store, config).pending()

    assert result["counts"] == {"ready": 1} and not result["configured"]
    assert result["alert"]["code"] == "insufficient_balance"
    with store() as session:
        account = session.get(TranslationAccountState, account_scope(config))
        assert account.code == "insufficient_balance" and account.observed_at == "2026-09-01T01:00:00+00:00"
        assert session.get(Translation, key).attempts == config.max_attempts


def test_read_only_status_does_not_reconcile_machine_results(store):
    config = TranslationConfig(enabled=True)
    key = seed(store, config)
    bind_article(store, key)
    before = snapshot(store, key)
    with store() as session:
        assert translation_status(session, config)["counts"] == {"review_required": 1}
        assert list(session.scalars(select(Translation.id))) == [cache_key(SOURCE, SOURCE, config)]
    assert snapshot(store, key) == before


@pytest.mark.asyncio
async def test_keyless_evidence_recovers_existing_cache_without_creating_missing_cache(store, monkeypatch):
    config = TranslationConfig(enabled=True)
    key = seed(store, config)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    existing = {"id": "existing", "title": SOURCE, "text": SOURCE}
    missing = {"id": "missing", "title": "AI tools are useful.", "text": "AI tools are useful."}

    result = await TranslationService(store, config).evidence([existing, missing])

    assert result == [{**existing, "title_zh": CANDIDATE, "text_zh": CANDIDATE}, missing]
    with store() as session:
        assert list(session.scalars(select(Translation.id))) == [key]
