"""Pure, read-only decisions for the persisted translation call ledger.

The ledger lives alongside each saved part. It never changes model receipts,
source text or candidate text. Unknown reservations are deliberately not retried.
"""

import hashlib
import json

VERSION = "translation-workflow-v1"
TRANSPORT_RETRIES = 2


def fingerprint(source, candidate):
    return hashlib.sha256(json.dumps([source, candidate], ensure_ascii=False).encode()).hexdigest()


def snapshots(part):
    yield part
    for item in part.get("review_history", []):
        previous = item.get("previous")
        if isinstance(previous, dict):
            yield from snapshots(previous)


def receipts(part, kind, policy, target=None):
    """Include all historical denials, including candidates in an A→B→A cycle."""
    for previous in snapshots(part):
        records = [previous.get("review" if kind == "correction" else "audit", {})]
        records += [r for r in previous.get("quality_history", []) if r.get("kind") == kind]
        for record in records:
            if record.get("policy") == policy and (target is None or record.get("fingerprint") == target):
                yield record


def concept_issues(record):
    """Preserve each explicit model objection even if its top-level verdict disagrees."""
    return [check.get('issue') or '术语“' + check.get('candidate_quote', '') + '”的含义或语境未通过核对'
            for check in record.get('concept_checks', [])
            if check.get('meaning_preserved') is not True or check.get('context_clear') is not True
            or check.get('issue')]


def rejected(record, *, audit=False):
    return (record.get("approved") is not True or bool(record.get("issues")) or
            (audit and bool(record.get("machine_issues") or record.get("correction_issues") or concept_issues(record))))


def audit_receipt(part, policy):
    target = fingerprint(part["source"], part.get("draft", part.get("zh", "")))
    records = list(receipts(part, "audit", policy, target))
    return next((r for r in records if rejected(r, audit=True)), records[-1] if records else None)


def denied(part, policy, target):
    return any(rejected(r, audit=True) for r in receipts(part, "audit", policy, target)) or any(
        rejected(r) for r in receipts(part, "correction", policy, target)
    )


def events(part, policy):
    source = hashlib.sha256(part["source"].encode()).hexdigest()
    return [e for e in part.get("workflow_history", [])
            if e.get("version") == VERSION and e.get("policy") == policy and e.get("source") == source]


def event(part, policy, kind, **values):
    return {"version": VERSION, "policy": policy, "source": hashlib.sha256(part["source"].encode()).hexdigest(),
            "kind": kind, **values}


def legacy_rounds(part):
    # Old service calls reset review.round; completed history is the stronger
    # evidence. Snapshots duplicate history, so take the maximum, not its sum.
    return max((max(
        len([r for r in p.get("quality_history", []) if r.get("kind") == "correction"]),
        max((r.get("round", 0) for r in [p.get("review", {})] + p.get("quality_history", [])
             if type(r.get("round", 0)) is int), default=0),
    ) for p in snapshots(part)), default=0)


def correction_rounds(part, policy):
    history = events(part, policy)
    baseline = next((e.get("correction_rounds", 0) for e in history if e["kind"] == "baseline"), None)
    if baseline is None:
        return legacy_rounds(part)
    results = {e["call_id"]: e for e in history if e["kind"] == "result"}
    return baseline + sum(
        1 for e in history if e["kind"] == "reserved" and e["stage"] == "correction"
        and results.get(e["call_id"], {}).get("outcome") not in {"known_balance", "known_transport"}
    )


def target_for(part, stage):
    return fingerprint(part["source"], "" if stage == "draft" else part.get("draft", part.get("zh", "")))


def blocked(part, policy):
    history = events(part, policy)
    completed = {e["call_id"] for e in history if e["kind"] == "result"}
    # A new review policy is not permission to replay an unknown old request.
    all_events = [e for previous in snapshots(part) for e in previous.get("workflow_history", [])
                  if e.get("version") == VERSION]
    finished = {e.get("call_id") for e in all_events if e.get("kind") == "result"}
    unknown = any(e.get("kind") == "reserved" and e.get("call_id") not in finished for e in all_events)
    return unknown or any(e["kind"] == "stop" for e in history) or any(
        e["kind"] == "reserved" and e["call_id"] not in completed for e in history
    )


def permitted(part, stage, policy, now, *, force=False, individual=False):
    if blocked(part, policy):
        return False
    target = target_for(part, stage)
    results = [e for e in events(part, policy)
               if e["kind"] == "result" and e.get("stage") == stage and e.get("target") == target]
    if not results:
        return True
    last = results[-1]
    if last["outcome"] == "not_started":
        return sum(e["outcome"] == "not_started" for e in results) <= TRANSPORT_RETRIES
    if last["outcome"] == "unknown" and last.get("code") == "technical_provider_error":
        return any(e.get("kind") == "launch_recovery" and e.get("call_id") == last["call_id"]
                   and e.get("stage") == stage and e.get("target") == target
                   and e.get("operator_confirmed_not_started") is True for e in events(part, policy))
    if stage == "correction" and last["outcome"] == "completed":
        return True  # A newly failed first audit may request one bounded repair.
    if last["outcome"] == "known_balance":
        return True
    if last["outcome"] == "known_transport":
        failures = sum(e["outcome"] == "known_transport" for e in results)
        return failures <= TRANSPORT_RETRIES and (force or last.get("retry_at", "") <= now)
    # A malformed multi-item mapping is a known unusable response. Only the
    # persisted single-item strategy may recover it; a second bad mapping stops.
    return last["outcome"] == "batch_mismatch" and individual


def next_stage(part, policy, max_rounds, now, *, force=False):
    if blocked(part, policy):
        return None
    candidate = part.get("draft", part.get("zh", ""))
    if not candidate:
        stage = "draft"
    else:
        receipt = audit_receipt(part, policy)
        if receipt is not None and not part.get("audit"):
            return "receipt"  # Pure receipt reuse must precede another request.
        if part.get("correction_required", bool(receipt and rejected(receipt, audit=True))
                    or not bool(part.get("zh"))):
            if correction_rounds(part, policy) >= max_rounds:
                return None
            stage = "correction"
        else:
            if receipt is not None:
                return None  # Already audited this exact candidate.
            stage = "audit"
    return stage if permitted(part, stage, policy, now, force=force,
                              individual=part.get("audit_mode") == "individual") else None


def seen_candidates(part, policy):
    found = {r.get("fingerprint") for kind in ("audit", "correction") for r in receipts(part, kind, policy)}
    for e in events(part, policy):
        if e["kind"] == "result" and e.get("stage") == "correction" and e.get("candidate_fingerprint"):
            found.add(e["candidate_fingerprint"])
    return found
