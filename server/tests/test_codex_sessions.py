"""Real local subprocesses simulate official JSONL; no Codex/model/auth calls."""

import asyncio
import fcntl
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from radar.codex_sessions import (
    CodexSessionError,
    CodexSessionFormatError,
    CodexSessionUnknown,
    request_fingerprint,
    run,
    scan_artifacts,
)
from radar.config import ProviderConfig

THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"
OTHER_THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a54"


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    accepted: bool
    batch_id: str


SCRIPT = r'''
import json, os, sys, time
from pathlib import Path
mode = sys.argv[1]
args = sys.argv[2:]
prompt = sys.stdin.read()
Path('invoked.json').write_text(json.dumps({'argv': args, 'pid': os.getpid(), 'prompt': prompt}))
thread = args[-2] if 'resume' in args else '0199a213-81c0-7800-8aa1-bbab2a035a53'
if mode == 'mismatch':
    thread = '0199a213-81c0-7800-8aa1-bbab2a035a54'
if mode == 'invalid_thread':
    thread = 'not-a-valid-uuid'
def send(value):
    raw = (json.dumps(value) + '\n').encode()
    if mode == 'fragmented':
        for index in range(0, len(raw), 7):
            os.write(1, raw[index:index+7])
    else:
        os.write(1, raw)
if mode == 'invalid_event':
    os.write(1, b'not-json\n')
    sys.exit(0)
send({'type': 'thread.started', 'thread_id': thread})
if mode == 'descendant':
    child = os.fork()
    if child == 0:
        time.sleep(100)
        os._exit(0)
    Path('child-pid.json').write_text(json.dumps({'pid':child}))
    os._exit(0)
if mode in ('wait', 'stderr_flood'):
    if mode == 'stderr_flood':
        while True:
            os.write(2, b'x' * 65536)
    time.sleep(100)
send({'type': 'turn.started'})
if mode == 'tool':
    send({'type': 'item.started', 'item': {'id': 'tool', 'type': 'command_execution'}})
if mode == 'line_flood':
    os.write(1, b'x' * 2_100_000)
    time.sleep(100)
if mode == 'stream_flood':
    for i in range(1000):
        send({'type': 'item.updated', 'item': {'id': 'think', 'type': 'reasoning', 'text': 'x' * 10000}})
if mode == 'multiple':
    send({'type': 'item.completed', 'item': {'id': 'commentary', 'type': 'agent_message', 'text': 'Working'}})
batch = prompt
text = json.dumps({'accepted': True, 'batch_id': batch})
if mode == 'bad_json':
    text = '{partial'
if mode == 'bad_schema':
    text = json.dumps({'accepted': 'yes', 'batch_id': batch})
send({'type': 'item.completed', 'item': {'id': 'final', 'type': 'agent_message', 'text': text}})
os.write(2, b'secret-private-stderr')
if mode == 'partial':
    os.write(1, b'{"type":"turn.complet')
    sys.exit(0)
if mode == 'no_completed':
    sys.exit(0)
send({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 5, 'secret': 'do-not-export'}})
if mode == 'after_completed':
    send({'type': 'turn.started'})
if mode == 'exit1':
    sys.exit(1)
'''


@pytest.fixture
def setup(tmp_path):
    script = tmp_path / "fake_codex.py"
    script.write_text(SCRIPT)

    def config(mode="ok"):
        return ProviderConfig(kind="codex", command=[sys.executable, str(script), mode], timeout_seconds=10)

    return tmp_path, config


def private_files(root):
    return [p for p in root.rglob("*") if p.name != "invoked.json"]


async def test_metering_uses_selected_model_and_counts_reused_receipt_once(setup, monkeypatch):
    from radar import usage

    root, config = setup
    monkeypatch.setenv("RADAR_USAGE_DATABASE_PATH", str(root / "usage.db"))
    monkeypatch.setenv("RADAR_CODEX_DEFAULT_MODEL", "gpt-test-default")
    work = root / "metered-batch"
    with usage.scope("discovery_foresight"):
        await run(config(), work, "batch-metered", Decision)
        recovered = await run(config(), work, "batch-metered", Decision)
    assert recovered.recovered
    args = json.loads((work / "invoked.json").read_text())["argv"]
    assert args[args.index("--model") + 1] == "gpt-test-default"
    report = usage.store().report("all")
    assert report["totals"]["calls"] == 1
    assert report["totals"]["total_tokens"] == 15
    assert report["groups"][0]["model"] == "gpt-test-default"
    assert report["groups"][0]["feature"] == "discovery_foresight"


@pytest.mark.asyncio
async def test_first_call_persists_thread_before_callback_and_uses_official_flags(setup):
    root, config = setup
    work = root / "batch-1"
    seen = []

    async def on_thread(uid):
        assert json.loads((work / "thread.json").read_text())["session_id"] == uid
        partial = list(work.glob("turn-*/events.partial"))[0]
        assert json.loads(partial.read_text().splitlines()[0])["thread_id"] == uid
        seen.append(uid)

    old = os.umask(0o077)
    try:
        result = await run(config(), work, "batch-1", Decision, on_thread=on_thread)
    finally:
        os.umask(old)
    assert result.session_id == THREAD and seen == [THREAD]
    assert Decision.model_validate_json(result.text).batch_id == "batch-1"
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}
    assert result.request_fingerprint == request_fingerprint(config(), "batch-1", Decision)
    args = json.loads((work / "invoked.json").read_text())["argv"]
    assert args[:5] == ["exec", "--sandbox", "read-only", "--color", "never"]
    for option in ["--json", "--ignore-user-config", "--output-schema", "features.shell_tool=false", 'web_search="disabled"']:
        assert option in args
    assert "--ephemeral" not in args and "resume" not in args and args[-1] == "-"
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    for path in private_files(work):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
        if path.is_file():
            assert b"secret-private-stderr" not in path.read_bytes()


@pytest.mark.asyncio
async def test_same_request_reuses_result_without_cli(setup, monkeypatch):
    root, config = setup
    work = root / "batch"
    first = await run(config(), work, "one", Decision)
    before = {p: p.read_bytes() for p in work.rglob("*") if p.is_file()}

    async def forbidden(*args, **kwargs):
        raise AssertionError("No second process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    second = await run(config(), work, "one", Decision)
    assert second.recovered and second.text == first.text
    assert scan_artifacts(work)[0] == second
    assert before == {p: p.read_bytes() for p in work.rglob("*") if p.is_file()}


@pytest.mark.asyncio
async def test_resume_in_new_batch_uses_exact_uuid_and_new_prompt(setup):
    root, config = setup
    first = await run(config(), root / "batch1", "one", Decision)
    second = await run(config(), root / "batch2", "two", Decision, session_id=first.session_id)
    assert second.session_id == first.session_id
    assert second.request_fingerprint != first.request_fingerprint
    assert json.loads(second.text)["batch_id"] == "two"
    args = json.loads((root / "batch2" / "invoked.json").read_text())["argv"]
    assert "resume" in args and args[-2:] == [THREAD, "-"] and "--last" not in args
    assert args.index("--sandbox") < args.index("resume") < args.index("--output-schema")


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", ["--last", "latest", "../other", "", "123", THREAD.upper()])
async def test_invalid_id_never_starts_process(setup, uid):
    root, config = setup
    with pytest.raises(CodexSessionError, match="invalid_session_id"):
        await run(config(), root / "batch", "one", Decision, session_id=uid)
    assert not list(root.rglob("invoked.json"))


@pytest.mark.asyncio
async def test_emitted_session_mismatch_is_unknown_and_never_bound(setup):
    root, config = setup
    work = root / "batch"
    with pytest.raises(CodexSessionUnknown, match="session_id_mismatch"):
        await run(config("mismatch"), work, "one", Decision, session_id=THREAD)
    assert not (work / "thread.json").exists() and scan_artifacts(work) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "fragmented", "multiple"])
async def test_official_stream_framing_and_final_message(setup, mode):
    root, config = setup
    result = await run(config(mode), root / mode, "batch", Decision)
    assert json.loads(result.text) == {"accepted": True, "batch_id": "batch"}
    assert scan_artifacts(root / mode)[0].text == result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [
    ("no_completed", "incomplete_turn"), ("partial", "incomplete_event_stream"),
    ("exit1", "nonzero_exit"), ("invalid_event", "invalid_event"),
    ("after_completed", "event_after_completion"), ("tool", "unexpected_tool_event"),
    ("invalid_thread", "invalid_thread_id"),
])
async def test_no_implicit_completion_and_uncertain_request_is_not_replayed(setup, mode, code):
    root, config = setup
    work = root / mode
    with pytest.raises(CodexSessionUnknown, match=code):
        await run(config(mode), work, "one", Decision)
    invoked = (work / "invoked.json").stat().st_mtime_ns
    assert scan_artifacts(work) == []
    with pytest.raises(CodexSessionError, match="previous_outcome_unresolved"):
        await run(config(mode), work, "one", Decision)
    assert (work / "invoked.json").stat().st_mtime_ns == invoked
    if (work / "thread.json").exists():
        with pytest.raises(CodexSessionError, match="previous_outcome_unresolved"):
            await run(config(mode), work, "different", Decision, session_id=THREAD)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bad_json", "bad_schema"])
async def test_complete_but_invalid_json_is_format_failure_not_valid_result(setup, mode):
    root, config = setup
    work = root / mode
    with pytest.raises(CodexSessionFormatError) as caught:
        await run(config(mode), work, "one", Decision)
    assert caught.value.failure_kind == "format" and not caught.value.outcome_unknown
    assert scan_artifacts(work, Decision) == []
    turn = next(work.glob("turn-*"))
    assert json.loads((turn / "exit.json").read_text())["returncode"] == 0
    assert "turn.completed" in (turn / "events.jsonl").read_text()


@pytest.mark.asyncio
async def test_callback_failure_preserves_thread_and_blocks_duplicate(setup):
    root, config = setup
    work = root / "batch"

    async def fail(uid):
        raise RuntimeError("private callback detail")

    with pytest.raises(CodexSessionUnknown) as caught:
        await run(config("wait"), work, "one", Decision, on_thread=fail)
    assert caught.value.session_id == THREAD and "private callback detail" not in str(caught.value)
    assert json.loads((work / "thread.json").read_text())["session_id"] == THREAD
    with pytest.raises(CodexSessionError, match="previous_outcome_unresolved"):
        await run(config("wait"), work, "one", Decision)


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_process(setup):
    root, config = setup
    work = root / "batch"
    cfg = config("wait").model_copy(update={"timeout_seconds": 1})
    with pytest.raises(CodexSessionUnknown, match="timeout_unknown"):
        await asyncio.wait_for(run(cfg, work, "one", Decision), 3)
    pid = json.loads((work / "invoked.json").read_text())["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert scan_artifacts(work) == []


@pytest.mark.asyncio
async def test_cancellation_kills_child_and_keeps_unknown_evidence(setup):
    root, config = setup
    work = root / "batch"
    seen = asyncio.Event()

    async def notify(uid):
        seen.set()

    task = asyncio.create_task(run(config("wait"), work, "one", Decision, on_thread=notify))
    await asyncio.wait_for(seen.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    pid = json.loads((work / "invoked.json").read_text())["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    failure = json.loads(next(work.glob("turn-*/failure.json")).read_text())
    assert failure["code"] == "cancelled_unknown" and failure["outcome_unknown"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [("line_flood", "event_line_limit"),
    ("stream_flood", "event_stream_limit"), ("stderr_flood", "stderr_size_limit")])
async def test_size_limits_terminate_stream_without_unbounded_buffers(setup, mode, code):
    root, config = setup
    work = root / mode
    with pytest.raises(CodexSessionUnknown, match=code):
        await asyncio.wait_for(run(config(mode), work, "one", Decision), 5)
    pid = json.loads((work / "invoked.json").read_text())["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert scan_artifacts(work) == []


@pytest.mark.asyncio
async def test_scan_recovers_completed_events_after_missing_result_write(setup):
    root, config = setup
    work = root / "batch"
    result = await run(config(), work, "one", Decision)
    Path(result.result_path).unlink()  # Synthetic crash after exit receipt, before atomic result.
    before = {p: p.read_bytes() for p in work.rglob("*") if p.is_file()}
    assert scan_artifacts(work) == []
    recovered = scan_artifacts(work, Decision)
    assert len(recovered) == 1 and recovered[0].text == result.text
    assert before == {p: p.read_bytes() for p in work.rglob("*") if p.is_file()}
    repeated = await run(config(), work, "one", Decision)
    assert repeated.recovered


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["result.json", "events.jsonl", "exit.json", "request.json"])
async def test_corrupt_artifact_never_becomes_cache_hit(setup, filename):
    root, config = setup
    work = root / "batch"
    result = await run(config(), work, "one", Decision)
    (work / result.turn_id / filename).write_text('{"partial":')
    assert scan_artifacts(work, Decision) == []
    with pytest.raises(CodexSessionError, match="previous_outcome_unresolved"):
        await run(config(), work, "one", Decision)


@pytest.mark.asyncio
async def test_distinct_prompt_schema_and_provider_do_not_reuse_another_result(setup):
    root, config = setup
    work = root / "batch"
    await run(config(), work, "one", Decision)
    with pytest.raises(CodexSessionError, match="explicit_resume_required"):
        await run(config(), work, "two", Decision)

    class Different(BaseModel):
        other: int

    assert scan_artifacts(work, Different) == []
    assert request_fingerprint(config(), "one", Different) != request_fingerprint(config(), "one", Decision)
    with pytest.raises(CodexSessionError, match="session_id_mismatch"):
        await run(config().model_copy(update={"model": "different"}), work, "two", Decision, session_id=THREAD)


@pytest.mark.asyncio
async def test_explicit_wrong_id_in_bound_directory_does_not_spawn(setup):
    root, config = setup
    work = root / "batch"
    await run(config(), work, "one", Decision)
    before = (work / "invoked.json").read_bytes()
    with pytest.raises(CodexSessionError, match="session_id_mismatch"):
        await run(config(), work, "two", Decision, session_id=OTHER_THREAD)
    assert (work / "invoked.json").read_bytes() == before


@pytest.mark.asyncio
async def test_inherited_external_flock_survives_parent_fd_close_until_child_exits(setup):
    root, config = setup
    lock = root / "owner.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    seen = asyncio.Event()

    async def notify(uid):
        seen.set()

    task = asyncio.create_task(run(config("wait"), root / "batch", "one", Decision,
                                   on_thread=notify, lock_fd=descriptor))
    await asyncio.wait_for(seen.wait(), 2)
    os.close(descriptor)  # Model child must still retain the same flock.
    check = os.open(lock, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(check)


@pytest.mark.asyncio
async def test_one_directory_allows_only_one_live_transport(setup):
    root, config = setup
    seen = asyncio.Event()

    async def notify(uid):
        seen.set()

    task = asyncio.create_task(run(config("wait"), root / "batch", "one", Decision, on_thread=notify))
    await asyncio.wait_for(seen.wait(), 2)
    with pytest.raises(CodexSessionError, match="session_busy"):
        await run(config("wait"), root / "batch", "two", Decision, session_id=THREAD)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_timeout_cleans_group_even_after_wrapper_has_exited(setup, monkeypatch):
    root, config = setup
    work = root / "batch"
    killed = []
    real_kill = os.killpg

    def kill_group(pid, sig):
        killed.append(pid)
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "killpg", kill_group)
    cfg = config("descendant").model_copy(update={"timeout_seconds": 1})
    with pytest.raises(CodexSessionUnknown, match="timeout_unknown"):
        await asyncio.wait_for(run(cfg, work, "one", Decision), 3)
    assert killed == [json.loads((work / "invoked.json").read_text())["pid"]]
    assert scan_artifacts(work) == []


@pytest.mark.asyncio
async def test_terminal_event_without_exit_receipt_cannot_recover(setup):
    root, config = setup
    work = root / "batch"
    result = await run(config(), work, "one", Decision)
    (work / result.turn_id / "exit.json").unlink()
    assert scan_artifacts(work, Decision) == []
    with pytest.raises(CodexSessionError, match="previous_outcome_unresolved"):
        await run(config(), work, "one", Decision)


@pytest.mark.asyncio
async def test_live_model_does_not_receive_unallowlisted_environment(setup, monkeypatch):
    root, config = setup
    monkeypatch.setenv("CODEX_TEST_PRIVATE_SECRET", "never-export")
    monkeypatch.setenv("CODEX_HOME", str(root / "canonical-auth-home"))
    called = {}
    real_spawn = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        called.update(kwargs)
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    await run(config(), root / "batch", "one", Decision)
    assert "CODEX_TEST_PRIVATE_SECRET" not in called["env"]
    assert called["env"]["CODEX_HOME"] == str(root / "canonical-auth-home")
    assert not (root / "canonical-auth-home").exists()  # Not read, copied, or created by transport.
