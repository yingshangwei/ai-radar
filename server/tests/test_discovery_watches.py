"""Use synthetic source/receipts; never supply editorial content to production."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from radar.config import DiscoveryConfig, RadarConfig
from radar.db import database
from radar.discovery import queue_candidate
from radar.discovery_watches import (
    apply_candidate,
    approved_signal,
    article_signal,
    discovery_status,
    list_entities,
    maintain_watches,
    on_watch_toggle,
    watch_metadata,
)
from radar.models import (
    Article,
    DiscoveryCall,
    DiscoveryEntity,
    DiscoveryWatch,
    Watch,
    now_iso,
)
from radar.pipeline import ingest
from radar.ranking import article_id
from radar.schemas import IncomingArticle


def source(uid="123", handle="labexample", **changes):
    values = dict(platform="x", external_id=uid, url=f"https://x.com/researcher/status/{uid}",
        author="Researcher", handle="researcher", title="New AI benchmark", published_at=datetime.now(UTC),
        text=f"We release a reproducible AI benchmark with open evaluation data. @{handle} produced the research code.",
        metrics={"like_count": 2}, entities=[dict(external_id="456" + uid, handle=handle, name="Example Lab",
            description="Research", followers_count=1, url=f"https://x.com/{handle}", relation="mention", matched_text=f"@{handle}")])
    return IncomingArticle(**(values | changes))


@pytest.fixture
def env(tmp_path):
    _, sessions = database(f"sqlite:///{tmp_path}/test.db")
    config = RadarConfig(discovery=DiscoveryConfig(enabled=True))
    config.reading.enabled = False
    return sessions, config


def receipt(session, config, item, *, hot=False, changes=None):
    row = queue_candidate(session, item, config, source_priority=hot)
    row.result = dict(candidate_id=row.id, is_ai_relevant=True, novelty=90, specificity=80,
        potential_impact=85, confidence=80, should_surface=True, reason_zh="对开放评测数据的潜力预判。",
        uncertainty_zh="尚需独立复现结果。", evidence_quotes=["reproducible AI benchmark"],
        entities=[dict(external_id=item.entities[0].external_id, kind="team", should_watch=True, confidence=90,
            reason_zh="原文明确关联了该团队的研究代码。", evidence_quote=f"@{item.entities[0].handle} produced the research code")], named_entities=[])
    row.result.update(changes or {})
    row.status, row.judged_at = "accepted", now_iso()
    session.add(DiscoveryCall(candidate_id=row.id, fingerprint=row.fingerprint, owner="synthetic",
        status="completed", result=json.loads(json.dumps(row.result)), completed_at=now_iso()))
    session.flush()
    return row


def apply(session, row, config):
    apply_candidate(session, row, config, lambda s, item, uid: ingest(s, [item], config,
        approved_signal=uid, queue_discovery=False))
    session.flush()


def test_approved_original_enters_normal_translation_without_fake_heat(env):
    sessions, config = env
    item = source()
    with sessions.begin() as session:
        assert ingest(session, [item], config) == 0
        row = receipt(session, config, item)
        apply(session, row, config)
        article = session.get(Article, article_id(item))
        assert article.text == item.text and article.title == item.title
        assert article.metrics == {"like_count": 2} and not article.priority
        assert "前瞻" in article.topics
        view = article_signal(session, article)
        assert view["initial_engagement"] == view["current_engagement"] == 2
        assert view["outcome"] == "pending"
        assert session.scalar(select(func.count()).select_from(DiscoveryWatch)) == 0
        apply(session, row, config)
        assert session.scalar(select(func.count()).select_from(Article)) == 1


@pytest.mark.parametrize("changes", [{"novelty": 74}, {"potential_impact": 74}, {"confidence": 74},
    {"specificity": 59}, {"should_surface": False}, {"is_ai_relevant": False}])
def test_rejected_or_weak_forecast_is_never_published(env, changes):
    sessions, config = env
    with sessions.begin() as session:
        row = receipt(session, config, source(), changes=changes)
        apply(session, row, config)
        assert not row.published_article_id
        assert session.scalar(select(func.count()).select_from(Article)) == 0


def test_forged_receipt_or_changed_source_cannot_bypass_filter(env):
    sessions, config = env
    item = source()
    with sessions.begin() as session:
        row = receipt(session, config, item)
        assert approved_signal(session, item, row.id, config)
        assert not approved_signal(session, item.model_copy(update={"text": "AI is great"}), row.id, config)
        row.result = row.result | {"confidence": 99}
        assert not approved_signal(session, item, row.id, config)
        with pytest.raises(ValueError):
            apply(session, row, config)


def test_real_metrics_refresh_keeps_forecast_without_new_judgment(env):
    sessions, config = env
    item = source()
    with sessions.begin() as session:
        row = receipt(session, config, item)
        apply(session, row, config)
        row.status = "done"
        ingest(session, [item.model_copy(update={"metrics": {"like_count": 12}})], config)
        article = session.get(Article, row.article_key)
        assert article.metrics["like_count"] == 12 and "前瞻" in article.topics
        ingest(session, [item.model_copy(update={"metrics": {"like_count": 120}})], config)
        view = article_signal(session, article)
        assert view["current_engagement"] == 120 and view["outcome"] == "gaining_attention"
        article.text = "Different AI text"
        assert article_signal(session, article) is None
        assert session.scalar(select(func.count()).select_from(DiscoveryCall)) == 1


def test_trial_has_verified_identity_bounded_duration_and_idempotence(env):
    sessions, config = env
    with sessions.begin() as session:
        row = receipt(session, config, source(), hot=True)
        apply(session, row, config)
        watch = session.get(Watch, "x:labexample")
        assert watch.enabled and watch.organization == ""
        trial = session.get(DiscoveryWatch, watch.id)
        assert trial.entity_id == "x:456123"
        assert 6 < (datetime.fromisoformat(trial.expires_at) - datetime.now(UTC)).total_seconds() / 86400 <= 7
        assert watch_metadata(session, watch)["status"] == "trial"
        apply(session, row, config)
        assert session.scalar(select(func.count()).select_from(DiscoveryWatch)) == 1
        assert len(session.get(DiscoveryEntity, trial.entity_id).evidence) == 1


def test_manual_disabled_and_existing_enabled_watches_are_never_taken_over(env):
    sessions, config = env
    with sessions.begin() as session:
        session.add(Watch(id="x:labexample", name="Kept", handle="labexample", enabled=False))
        for handle in ["labexample", "OpenAI"]:
            row = receipt(session, config, source(handle=handle), hot=True)
            apply(session, row, config)
        assert not session.get(Watch, "x:labexample").enabled
        assert session.get(Watch, "x:openai").enabled
        assert session.scalar(select(func.count()).select_from(DiscoveryWatch)) == 0


def test_daily_and_active_watch_caps_preserve_candidates(env):
    sessions, config = env
    config.discovery.max_auto_watches = 1
    with sessions.begin() as session:
        for index in range(3):
            row = receipt(session, config, source(str(index), f"lab{index}"), hot=True)
            apply(session, row, config)
        assert session.scalar(select(func.count()).select_from(DiscoveryWatch)) == 1
        assert discovery_status(session, config)["entities_pending"] == 2
        assert len(list_entities(session, 1)) == 1


def test_expiry_is_automatic_but_user_choice_is_preserved(env):
    sessions, config = env
    with sessions.begin() as session:
        for index in range(2):
            apply(session, receipt(session, config, source(str(index), f"lab{index}"), hot=True), config)
        watch = session.get(Watch, "x:lab0")
        on_watch_toggle(session, watch, True)
        for trial in session.scalars(select(DiscoveryWatch)):
            trial.expires_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        maintain_watches(session, config)
        assert watch.enabled and watch_metadata(session, watch)["status"] == "retained"
        assert not session.get(Watch, "x:lab1").enabled
        assert watch_metadata(session, session.get(Watch, "x:lab1"))["status"] == "expired"
        watch.enabled = False
        on_watch_toggle(session, watch, False)
        assert watch_metadata(session, watch)["status"] == "user_stopped"


def test_explicit_name_without_identity_stays_unresolved(env):
    sessions, config = env
    with sessions.begin() as session:
        row = receipt(session, config, source(), hot=True, changes={"entities": [], "named_entities": [
            dict(name="AI benchmark", kind="team", reason_zh="原文明确出现的研究名称，身份尚不确定。", evidence_quote="reproducible AI benchmark")]})
        apply(session, row, config)
        assert session.scalar(select(func.count()).select_from(DiscoveryWatch)) == 0
        assert list_entities(session)[0]["status"] == "identity_unresolved"


def test_changed_handle_or_reused_handle_pauses_only_managed_watch(env):
    sessions, config = env
    with sessions.begin() as session:
        item = source()
        apply(session, receipt(session, config, item, hot=True), config)
        renamed = source("987", handle="newlab")
        renamed.entities[0].external_id = item.entities[0].external_id
        apply(session, receipt(session, config, renamed, hot=True), config)
        assert not session.get(Watch, "x:labexample").enabled
        assert not session.get(Watch, "x:newlab")
        assert session.get(DiscoveryEntity, "x:" + item.entities[0].external_id).status == "identity_changed"


def test_reassigned_handle_cannot_silently_replace_watched_identity(env):
    sessions, config = env
    with sessions.begin() as session:
        apply(session, receipt(session, config, source(), hot=True), config)
        apply(session, receipt(session, config, source("222"), hot=True), config)
        assert not session.get(Watch, "x:labexample").enabled
        assert session.get(DiscoveryEntity, "x:456222").status == "identity_conflict"


def test_non_ai_source_update_invalidates_waiting_judgment(env):
    sessions, config = env
    item = source()
    with sessions.begin() as session:
        row = receipt(session, config, item)
        changed = item.model_copy(update={"title": "A garden", "text": "Flowers are blooming in the garden.", "entities": []})
        assert ingest(session, [changed], config) == 0
        assert row.status == "superseded" and row.error_code == "source_changed"
        assert not approved_signal(session, item, row.id, config)


def test_later_hot_metrics_apply_saved_entity_advice_without_new_model_call(env):
    sessions, config = env
    item = source()
    with sessions.begin() as session:
        row = receipt(session, config, item)
        apply(session, row, config)
        row.status = "done"
        assert not session.get(Watch, "x:labexample")
        ingest(session, [item.model_copy(update={"metrics": {"like_count": 150}})], config)
        assert row.status == "accepted" and row.seed_qualified
        apply(session, row, config)
        assert session.get(Watch, "x:labexample").enabled
        assert session.scalar(select(func.count()).select_from(DiscoveryCall)) == 1


def test_waiting_recommendation_promotes_when_trial_capacity_releases(env):
    sessions, config = env
    config.discovery.max_auto_watches = 1
    with sessions.begin() as session:
        for index in range(2):
            apply(session, receipt(session, config, source(str(index), f"lab{index}"), hot=True), config)
        assert not session.get(Watch, "x:lab1")
        session.get(DiscoveryWatch, "x:lab0").expires_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        maintain_watches(session, config)
        assert not session.get(Watch, "x:lab0").enabled
        assert session.get(Watch, "x:lab1").enabled
        assert session.scalar(select(func.count()).select_from(DiscoveryCall)) == 2
