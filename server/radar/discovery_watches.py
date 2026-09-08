"""Apply source-grounded discovery judgments without rewriting source content."""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from .discovery_contracts import validate_decision
from .models import (
    Article,
    DiscoveryCall,
    DiscoveryCandidate,
    DiscoveryEntity,
    DiscoveryWatch,
    Watch,
    now_iso,
)
from .ranking import article_id, engagement
from .schemas import IncomingArticle


def _same_content(payload, article):
    return all(payload.get(key, "") == getattr(article, key) for key in (
        "platform", "external_id", "url", "title", "text", "author", "handle",
    )) and datetime.fromisoformat(payload["published_at"]) == (
        article.published_at if isinstance(article.published_at, datetime)
        else datetime.fromisoformat(article.published_at)
    )


def _decision(session, row):
    if not row or row.status not in {"accepted", "done"} or row.error_code == "source_changed":
        return None
    # Publication requires the durable, successful receipt for this exact evidence.
    call = session.scalar(select(DiscoveryCall).where(
        DiscoveryCall.candidate_id == row.id, DiscoveryCall.fingerprint == row.fingerprint,
        DiscoveryCall.status == "completed",
    ).order_by(DiscoveryCall.created_at.desc()).limit(1))
    if call is None or call.result != row.result:
        return None
    import json

    try:
        return validate_decision(json.dumps(row.result, ensure_ascii=False), row.id, row.payload)
    except ValueError:
        return None


def _surface(row, decision, config):
    settings = config.discovery
    return bool(row.low_engagement and decision and decision.is_ai_relevant and decision.should_surface
        and decision.novelty >= settings.early_score_min
        and decision.potential_impact >= settings.early_score_min
        and decision.specificity >= 60 and decision.confidence >= settings.early_confidence_min)


def approved_signal(session, item, decision_id, config):
    row = session.get(DiscoveryCandidate, decision_id) if decision_id else None
    if not row or row.article_key != article_id(item) or not _same_content(row.payload, item):
        return False
    return _surface(row, _decision(session, row), config)


def article_signal(session, article):
    rows = session.scalars(select(DiscoveryCandidate).where(
        DiscoveryCandidate.published_article_id == article.id,
        DiscoveryCandidate.status.in_(("accepted", "done")),
    ).order_by(DiscoveryCandidate.created_at.desc()))
    for row in rows:
        if not _same_content(row.payload, article) or not _decision(session, row):
            continue
        score = max(0, engagement(article.metrics))
        result = row.result
        return {"kind": "early_signal", "label": "潜力预判", "reason_zh": result["reason_zh"],
            "uncertainty_zh": result["uncertainty_zh"], "confidence": result["confidence"],
            "potential_impact": result["potential_impact"], "novelty": result["novelty"],
            "judged_at": row.judged_at, "initial_engagement": row.initial_engagement,
            "current_engagement": score,
            "outcome": "gaining_attention" if score >= max(30, row.initial_engagement * 3) else "pending"}
    return None


def _evidence(row, quote):
    return {"candidate_id": row.id, "url": row.payload["url"], "author": row.payload["author"],
        "handle": row.payload.get("handle", ""), "quote": quote, "observed_at": row.judged_at}


def _append_evidence(existing, entry):
    return [e for e in (existing or []) if e.get("candidate_id") != entry["candidate_id"]][-19:] + [entry]


def maintain_watches(session, config):
    """Trial expiry never changes a watch that the user has taken over."""
    now = now_iso()
    for trial in session.scalars(select(DiscoveryWatch).where(
        DiscoveryWatch.managed.is_(True), DiscoveryWatch.status == "trial", DiscoveryWatch.expires_at <= now,
    )):
        watch = session.get(Watch, trial.watch_id)
        if watch:
            watch.enabled = False
        trial.status = "expired"
        entity = session.get(DiscoveryEntity, trial.entity_id)
        if entity:
            entity.status, entity.updated_at = "expired", now
    if not config.discovery.enabled:
        return
    # A daily cap delays a grounded recommendation; it should not require a
    # fresh model judgment or a manual click after capacity becomes available.
    for entity in session.scalars(select(DiscoveryEntity).where(
        DiscoveryEntity.status == "candidate", DiscoveryEntity.handle != "",
        DiscoveryEntity.confidence >= config.discovery.watch_confidence_min,
    ).order_by(DiscoveryEntity.first_seen_at).limit(50)):
        for evidence in reversed(entity.evidence or []):
            candidate = session.get(DiscoveryCandidate, evidence.get("candidate_id", ""))
            if not candidate or not candidate.seed_qualified or not _decision(session, candidate):
                continue
            if datetime.fromisoformat(candidate.payload["published_at"]) < (
                datetime.now(UTC) - timedelta(hours=config.discovery.max_age_hours)
            ):
                continue
            _trial_watch(session, entity, evidence, config)
            break


def _trial_watch(session, entity, evidence, config):
    settings = config.discovery
    existing = session.scalar(select(Watch).where(Watch.platform == "x", func.lower(Watch.handle) == entity.handle.lower()))
    owned = session.scalar(select(DiscoveryWatch).where(DiscoveryWatch.entity_id == entity.id))
    if owned and owned.managed:
        old = session.get(Watch, owned.watch_id)
        if old and old.handle.lower() != entity.handle.lower():
            old.enabled = False
            owned.status, owned.expires_at = "expired", now_iso()
            entity.status = "identity_changed"
            return
    if existing:
        previous = session.get(DiscoveryWatch, existing.id)
        if previous and previous.managed and previous.entity_id != entity.id:
            existing.enabled = False
            previous.status, previous.expires_at = "expired", now_iso()
            entity.status = "identity_conflict"
            return
        # An existing user choice (including disabled/wrong-name corrections) wins forever.
        entity.status = "already_watched" if existing.enabled else "user_stopped"
        if owned and owned.watch_id == existing.id and owned.managed and owned.status == "trial" and existing.enabled:
            entity.status = "trial"
            owned.evidence = _append_evidence(owned.evidence, evidence)
        return
    if owned:
        entity.status = "identity_changed"
        return
    active = session.scalar(select(func.count()).select_from(DiscoveryWatch).where(
        DiscoveryWatch.managed.is_(True), DiscoveryWatch.status == "trial"))
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    daily = session.scalar(select(func.count()).select_from(DiscoveryWatch).where(DiscoveryWatch.created_at >= today))
    total = session.scalar(select(func.count()).select_from(Watch))
    if not settings.auto_watch or active >= settings.max_auto_watches or daily >= settings.max_new_watches_per_day or total >= 100:
        entity.status = "candidate"
        return
    uid = "x:" + entity.handle.lower()
    if session.get(Watch, uid):
        entity.status = "identity_conflict"
        return
    watch = Watch(id=uid, name=entity.name[:120], handle=entity.handle, platform="x",
        organization="", role="自动试关注", enabled=True)
    session.add(watch)
    session.flush()
    session.add(DiscoveryWatch(watch_id=uid, entity_id=entity.id, status="trial", managed=True,
        expires_at=(datetime.now(UTC) + timedelta(days=settings.trial_days)).isoformat(), evidence=[evidence]))
    entity.status = "trial"
    session.flush()


def apply_candidate(session, candidate, config, publish):
    decision = _decision(session, candidate)
    if decision is None:
        raise ValueError("Discovery has no valid completed source-grounded receipt")
    maintain_watches(session, config)
    item = IncomingArticle.model_validate(candidate.payload)
    if item.published_at < datetime.now(UTC) - timedelta(hours=config.discovery.max_age_hours):
        return
    if _surface(candidate, decision, config):
        existing = session.get(Article, candidate.article_key)
        # A queued decision cannot roll back a separately refreshed article.
        if not existing or _same_content(candidate.payload, existing):
            observed = item.model_copy(update={"metrics": dict(candidate.latest_metrics)})
            publish(session, observed, candidate.id)
            article = session.get(Article, candidate.article_key)
            if article and _same_content(candidate.payload, article):
                candidate.published_article_id = article.id
    if not candidate.seed_qualified or not decision.is_ai_relevant:
        return
    profiles = {e.external_id: e for e in item.entities}
    for recommendation in decision.entities:
        if not recommendation.should_watch or recommendation.confidence < config.discovery.watch_confidence_min:
            continue
        profile = profiles[recommendation.external_id]
        uid = "x:" + profile.external_id
        entity = session.get(DiscoveryEntity, uid)
        if entity is None:
            entity = DiscoveryEntity(id=uid, name=profile.name, handle=profile.handle, evidence=[])
            session.add(entity)
        entity.kind, entity.name, entity.handle = recommendation.kind, profile.name, profile.handle
        entity.profile = profile.model_dump(mode="json")
        entity.confidence, entity.reason_zh = recommendation.confidence, recommendation.reason_zh
        entity.updated_at = now_iso()
        evidence = _evidence(candidate, recommendation.evidence_quote)
        entity.evidence = _append_evidence(entity.evidence, evidence)
        session.flush()
        _trial_watch(session, entity, evidence, config)
    for recommendation in decision.named_entities:
        uid = "named:" + hashlib.sha256(recommendation.name.casefold().encode()).hexdigest()
        entity = session.get(DiscoveryEntity, uid)
        if entity is None:
            entity = DiscoveryEntity(id=uid, name=recommendation.name, handle="", evidence=[])
            session.add(entity)
        entity.kind, entity.reason_zh = recommendation.kind, recommendation.reason_zh
        entity.status, entity.updated_at = "identity_unresolved", now_iso()
        entity.evidence = _append_evidence(entity.evidence, _evidence(candidate, recommendation.evidence_quote))


def watch_metadata(session, watch):
    trial = session.get(DiscoveryWatch, watch.id)
    if not trial:
        return None
    entity = session.get(DiscoveryEntity, trial.entity_id)
    return {"origin": "automatic", "status": trial.status, "expires_at": trial.expires_at,
        "reason_zh": entity.reason_zh if entity else "由近期 AI 信息发现。"}


def on_watch_toggle(session, watch, enabled):
    trial = session.get(DiscoveryWatch, watch.id)
    if trial:
        trial.managed, trial.status = False, "retained" if enabled else "user_stopped"
        entity = session.get(DiscoveryEntity, trial.entity_id)
        if entity:
            entity.status, entity.updated_at = trial.status, now_iso()


def discovery_status(session, config):
    from .discovery import OPEN, DiscoveryService

    status = DiscoveryService(None, config, None).status(session)
    status.update(pending=sum(status["counts"].get(s, 0) for s in OPEN),
        unknown=status["counts"].get("unknown", 0), error=status["counts"].get("needs_attention", 0),
        daily_call_limit=config.discovery.max_calls_per_day,
        active_trials=session.scalar(select(func.count()).select_from(DiscoveryWatch).where(
            DiscoveryWatch.managed.is_(True), DiscoveryWatch.status == "trial", DiscoveryWatch.expires_at > now_iso())),
        entities_pending=session.scalar(select(func.count()).select_from(DiscoveryEntity).where(
            DiscoveryEntity.status.in_(("candidate", "identity_unresolved", "identity_changed", "identity_conflict")))))
    return status


def list_entities(session, limit=50):
    return [{"id": row.id, "name": row.name, "handle": row.handle, "kind": row.kind,
        "status": row.status, "reason_zh": row.reason_zh, "confidence": row.confidence,
        "evidence": row.evidence, "updated_at": row.updated_at}
        for row in session.scalars(select(DiscoveryEntity).order_by(DiscoveryEntity.updated_at.desc()).limit(min(100, max(1, limit))))]
