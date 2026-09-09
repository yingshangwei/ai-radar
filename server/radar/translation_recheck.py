"""Server translation operations using the normal translation service.

Operations accept identifiers or batch limits, never replacement prose. They
do not initialize Pipeline, which recovers interrupted jobs on server startup.
"""

import asyncio
import hashlib
import json
import shutil
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import Text, cast, or_, select

from . import translation_workflow as workflow
from .config import TranslationConfig, secret
from .models import Article, ArticleTranslation, Job, Translation, now_iso
from .translation import (
    RECHECK_POLICY,
    TranslationService,
    cache_key,
    needs_recheck,
    review_policy,
    translation_status,
)


def confirm_unstarted_cli(row, config, call_id, original_path):
    """Explicit operator diagnosis; never infer non-execution from an expired lease.

    The operator must verify the original launch environment and zero execution.
    We additionally require a missing program in that PATH, a working current
    program, one exact terminal CLI failure, and no returned response/denial.
    This only appends a receipt; source, candidate, old results and budgets stay.
    """
    if (str(UUID(call_id)) != call_id or not isinstance(original_path, str) or not original_path
            or any(not Path(p).is_absolute() for p in original_path.split(':'))):
        raise ValueError("启动诊断参数无效。")
    provider = config.technical_review_provider
    if (not provider or provider.kind not in {"codex", "claude_cli", "command"} or not provider.command
            or shutil.which(provider.command[0], path=original_path) is not None
            or shutil.which(provider.command[0]) is None):
        raise ValueError("必须确认原启动 PATH 缺少该程序，且当前环境已修复。")
    if row.status != "error" or row.owner or row.lease_until >= now_iso() or review_policy(row) != RECHECK_POLICY:
        raise ValueError("只能诊断已结束且无占用的当前技术翻译错误。")
    parts = deepcopy(row.parts)
    matched = [p for p in parts if any(e.get('call_id') == call_id for e in workflow.events(p, RECHECK_POLICY))]
    if not matched:
        raise ValueError("指定调用不属于当前选中的翻译。")
    for part in matched:
        history = workflow.events(part, RECHECK_POLICY)
        calls = [e for e in history if e.get('call_id') == call_id]
        results = [e for e in history if e.get('kind') == 'result']
        requests = [e for e in calls if e.get('kind') == 'request_reserved']
        last = results[-1] if results else {}
        stage, target = last.get('stage'), last.get('target')
        if (last.get('call_id') != call_id or last.get('outcome') != 'unknown'
                or last.get('code') != 'technical_provider_error' or stage not in {'correction', 'audit'}
                or target != workflow.target_for(part, stage) or len(requests) != 1
                or requests[0].get('transport') != provider.kind
                or any(e.get('kind') in {'request_returned', 'launch_recovery'} for e in calls)
                or workflow.blocked(part, RECHECK_POLICY) or workflow.denied(part, RECHECK_POLICY, target)
                or workflow.correction_rounds(part, RECHECK_POLICY) >= config.review_max_rounds):
            raise ValueError("没有可确认的单次启动失败；未知执行、已返回结果、否决和已耗尽预算均不能恢复。")
        part['workflow_history'].append(workflow.event(part, RECHECK_POLICY, 'launch_recovery',
            call_id=call_id, stage=stage, target=target, at=now_iso(), operator_confirmed_not_started=True,
            original_path_sha256=hashlib.sha256(original_path.encode()).hexdigest(),
            command_sha256=hashlib.sha256(json.dumps(provider.command).encode()).hexdigest()))
    row.parts = parts


def confirm_stopped_cli_timeout(row, config, call_id, previous_timeout):
    """Operator-confirmed terminal timeout from the old opaque CLI error wrapper.

    No verdict was delivered. Keep the unknown receipt, candidate and semantic
    budget, and count the confirmed timeout against the normal transport cap.
    This is never inferred from a missing result or an expired lease alone.
    """
    provider = config.technical_review_provider
    if (str(UUID(call_id)) != call_id or type(previous_timeout) is not int or not 10 <= previous_timeout <= 900
            or not provider or provider.kind not in {'codex', 'claude_cli'}
            or provider.timeout_seconds < previous_timeout):
        raise ValueError('须核实原 CLI 超时设置及进程组已终止，仅支持禁用工具的 Agent 适配器。')
    if row.status != 'error' or row.owner or row.lease_until >= now_iso() or review_policy(row) != RECHECK_POLICY:
        raise ValueError('只能诊断已结束且无占用的当前技术翻译错误。')
    parts = deepcopy(row.parts)
    matched = [p for p in parts if any(e.get('call_id') == call_id for e in workflow.events(p, RECHECK_POLICY))]
    if not matched:
        raise ValueError('调用不属于当前选中缓存。')
    for part in matched:
        history = workflow.events(part, RECHECK_POLICY)
        calls = [e for e in history if e.get('call_id') == call_id]
        results = [e for e in history if e.get('kind') == 'result']
        requests = [e for e in calls if e.get('kind') == 'request_reserved']
        last = results[-1] if results else {}
        stage, target = last.get('stage'), last.get('target')
        if (last.get('call_id') != call_id or last.get('outcome') != 'unknown'
                or last.get('code') != 'technical_provider_error' or stage not in {'correction', 'audit'}
                or target != workflow.target_for(part, stage) or len(requests) != 1
                or requests[0].get('transport') != provider.kind
                or requests[0].get('timeout_seconds', previous_timeout) != previous_timeout
                or any(e.get('kind') in {'request_returned', 'timeout_recovery', 'launch_recovery'} for e in calls)
                or workflow.blocked(part, RECHECK_POLICY) or workflow.denied(part, RECHECK_POLICY, target)
                or workflow.transport_failures(part, stage, RECHECK_POLICY, target) >= workflow.TRANSPORT_RETRIES):
            raise ValueError('只允许有剩余传输重试次数、无输出/否决的精确超时调用。')
        started, ended = datetime.fromisoformat(requests[0]['at']), datetime.fromisoformat(last['at'])
        if not started.tzinfo or not ended.tzinfo or not previous_timeout <= (ended-started).total_seconds() < previous_timeout+30:
            raise ValueError('调用持续时间与核实的 CLI 超时不对应，不能据此推断已终止。')
        part['workflow_history'].append(workflow.event(part, RECHECK_POLICY, 'timeout_recovery',
            call_id=call_id, stage=stage, target=target, at=now_iso(), operator_confirmed_stopped_timeout=True,
            previous_timeout_seconds=previous_timeout, retry_at=(ended+timedelta(seconds=60)).isoformat()))
    row.parts = parts


async def translate_limited(
    sessions, config: TranslationConfig, *, limit: int, force=False, errors_only=False, machine_only=False,
):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("翻译批次上限必须是 1 到 500 之间的整数。")
    if not config.enabled:
        raise ValueError("翻译功能尚未启用，未开始翻译。")
    if machine_only and (force or errors_only):
        raise ValueError("机器校验复检不能与强制翻译或仅错误缓存模式合用。")
    limited = config.model_copy(deep=True, update={"max_documents": min(limit, config.max_documents)})
    with sessions.begin() as session:
        # Serialize CLI reservations, without changing API startup recovery or
        # claiming a global API/CLI lock. Translation row leases remain final.
        if session.bind.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if session.scalar(select(Job.id).where(Job.status == "running").limit(1)):
            raise RuntimeError("已有任务正在运行，请在服务空闲后执行限量翻译。")
        if not machine_only and not secret(config.api_key_env):
            raise ValueError("翻译服务尚未配置凭据，未开始翻译。")
        scope = "错误状态的翻译缓存" if errors_only else "翻译缓存"
        job = Job(kind="translate", message=(
            f"机器校验复检最多处理 {limited.max_documents} 份缓存，复用已有模型审核，不调用模型。"
            if machine_only else f"本轮最多处理 {limited.max_documents} 份{scope}。"
        ))
        session.add(job)
        session.flush()
        uid = job.id

    failure = ""
    machine_revalidated = 0
    try:
        with sessions() as session:
            if session.scalar(select(Job.id).where(Job.status == "running", Job.id != uid).limit(1)):
                raise RuntimeError("另一个任务已开始，未开始限量翻译。")
        service = TranslationService(sessions, limited)
        if machine_only:
            checked = await service.pending(machine_only=True)
            changed = checked["machine_revalidated"]
            if type(changed) is not int or not 0 <= changed <= limited.max_documents:
                raise ValueError("机器校验复检数量无效。")
            machine_revalidated = changed
        elif errors_only:
            await service.pending(force=force, errors_only=True)
        else:
            await service.pending(force=force)
    except BaseException as exc:
        # Provider exceptions can include credentials and source/candidate text.
        failure = (
            "机器校验复检任务中断；原文和已有模型审核记录保留，可在服务空闲后重试。"
            if machine_only else "限量翻译任务中断；原文和已保存进度保留，可在服务空闲后重试。"
        )
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
    finally:
        with sessions.begin() as session:
            translated = translation_status(session, config)
            counts, resources = translated["counts"], translated["resource_counts"]
            waiting = sum(n for status, n in counts.items() if status != "ready")
            resource_waiting = sum(n for status, n in resources.items() if status != "ready")
            if machine_only:
                message = "" if failure else (
                    f"机器校验复检完成 {machine_revalidated} 份缓存，本轮上限 {limited.max_documents} 份；"
                    "复用已有模型审核，未调用模型。"
                )
            else:
                message = f"本轮最多处理 {limited.max_documents} 份{scope}。"
            message += f"主消息中文版本 {counts.get('ready', 0)} 条"
            message += f"，{waiting} 条仍在等待翻译或校对。" if waiting else "。"
            message += f"网页正文中文版本 {resources.get('ready', 0)} 份"
            message += f"，{resource_waiting} 份仍在等待翻译或校对。" if resource_waiting else "。"
            job = session.get(Job, uid)
            job.status = "failed" if failure else "completed"
            job.message, job.finished_at = failure + message, now_iso()
            result = {
                "job_id": uid, "status": job.status, "message": job.message,
                "limit": limited.max_documents, "counts": counts, "resource_counts": resources,
                "alert": translated["alert"],
            }
            if errors_only:
                result["errors_only"] = True
            if machine_only:
                result["machine_only"] = True
                result["machine_revalidated"] = machine_revalidated
    return result


def has_editorial_provenance(row: Translation) -> bool:
    # History is retained for accountability, but is not an active override.
    if "editorial" in row.review_model.casefold():
        return True
    for part in row.parts:
        if not isinstance(part, dict):
            continue
        if any(key.startswith("editorial_") for key in part):
            return True
        if part.get("recheck_pending"):
            for history in part.get("review_history", []):
                if not isinstance(history, dict):
                    continue
                previous = history.get("previous", {})
                provenance = history.get("row_provenance", {})
                if any(key.startswith("editorial_") for key in previous) or "editorial" in (
                    provenance.get("review_model", "").casefold()
                ):
                    return True
    return False


def select_rechecks(session, config: TranslationConfig, *, article_ids=(), editorial=False):
    ids = list(dict.fromkeys(article_ids))
    if bool(ids) == bool(editorial):
        raise ValueError("请指定文章 ID 或历史编辑复核范围，二者只能选择一个。")
    selected = {}
    if ids:
        articles = {row.id: row for row in session.scalars(select(Article).where(Article.id.in_(ids)))}
        if set(ids) - articles.keys():
            raise ValueError("指定的文章不存在；未开始复核。")
        for uid in ids:
            binding = session.get(ArticleTranslation, uid)
            row = session.get(Translation, binding.translation_id) if binding else None
            article = articles[uid]
            if row is None or row.id != cache_key(article.title, article.text, config):
                raise ValueError("指定文章尚无当前配置的翻译缓存，请先完成正常采集翻译。")
            selected[row.id] = row
    else:
        candidates = session.scalars(select(Translation).where(or_(
            Translation.review_model.ilike("%editorial%"),
            cast(Translation.parts, Text).contains('"editorial_'),
            cast(Translation.parts, Text).contains('"recheck_pending"'),
        )).order_by(Translation.id))
        selected = {row.id: row for row in candidates if has_editorial_provenance(row)}
    return list(selected.values())


def _reserve_job(sessions, config, *, article_ids, editorial, force, unstarted_call=None, original_cli_path=None,
                 stopped_timeout_call=None, previous_cli_timeout=None):
    with sessions.begin() as session:
        # Serialize CLI reservations on the deployed SQLite database. Per-row
        # translation leases remain authoritative across API/CLI processes.
        if session.bind.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if session.scalar(select(Job.id).where(Job.status == "running").limit(1)):
            raise RuntimeError("已有任务正在运行，请在服务空闲后执行复核。")
        rows = select_rechecks(session, config, article_ids=article_ids, editorial=editorial)
        if stopped_timeout_call or previous_cli_timeout is not None:
            if not stopped_timeout_call or previous_cli_timeout is None or editorial or len(rows) != 1 or unstarted_call:
                raise ValueError('超时诊断须指定一篇文章、一个调用 ID 和已核实的原超时设置。')
            confirm_stopped_cli_timeout(rows[0], config, stopped_timeout_call, previous_cli_timeout)
        if unstarted_call or original_cli_path:
            if not unstarted_call or not original_cli_path or editorial or len(rows) != 1:
                raise ValueError("启动诊断须指定一篇文章、一个未启动调用 ID 及原启动 PATH。")
            confirm_unstarted_cli(rows[0], config, unstarted_call, original_cli_path)
        keys = [row.id for row in rows]
        pending = [row.id for row in rows if force or needs_recheck(row)]
        if pending and not secret(config.api_key_env):
            raise ValueError("翻译服务尚未配置凭据，未开始复核。")
        if any(row.id in pending and row.lease_until >= now_iso() for row in rows):
            raise RuntimeError("选中的翻译仍被其他任务处理，未开始复核。")
        job = Job(kind="translate", message=f"正在按原文重新复核 {len(pending)} 份翻译缓存。")
        session.add(job)
        session.flush()
        return job.id, keys, pending


async def recheck_translations(sessions, config: TranslationConfig, *, article_ids=(), editorial=False, force=False,
                              unstarted_call=None, original_cli_path=None,
                              stopped_timeout_call=None, previous_cli_timeout=None):
    if not config.enabled:
        raise ValueError("翻译功能尚未启用，未开始复核。")
    uid, selected, pending = _reserve_job(
        sessions, config, article_ids=article_ids, editorial=editorial, force=force,
        unstarted_call=unstarted_call, original_cli_path=original_cli_path,
        stopped_timeout_call=stopped_timeout_call, previous_cli_timeout=previous_cli_timeout,
    )
    service = TranslationService(sessions, config)
    failure = ""
    try:
        for key in pending:
            with sessions() as session:
                if session.scalar(select(Job.id).where(Job.status == "running", Job.id != uid).limit(1)):
                    raise RuntimeError("另一个任务已开始；尚未处理的记录保持原样，请稍后重新复核。")
            await service.translate_one(key, force=force, recheck=True)
            if service.balance_blocked:
                break
    except BaseException as exc:
        # Never persist provider exceptions: they may contain source text or credentials.
        failure = "复核任务中断；原文和已保存进度保留，可在服务空闲后重试。"
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
    finally:
        with sessions.begin() as session:
            rows = list(session.scalars(select(Translation).where(Translation.id.in_(selected))))
            remaining = len(selected) - len(rows) + sum(
                row.status != "ready" or needs_recheck(row) for row in rows
            )
            job = session.get(Job, uid)
            job.status = "failed" if failure or remaining else "completed"
            job.message = failure or (
                f"原文复核完成：{len(selected) - remaining} 份通过当前校对规则，"
                f"{remaining} 份待复核；{len(selected) - len(pending)} 份已校对缓存直接复用。"
            )
            job.finished_at = now_iso()
            result = {
                "job_id": uid, "status": job.status, "policy": RECHECK_POLICY,
                "selected": len(selected), "skipped": len(selected) - len(pending),
                "ready": len(selected) - remaining, "remaining": remaining,
                "translation_ids": selected,
            }
    return result
