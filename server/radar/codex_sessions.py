"""Durable Codex exec/resume transport; the caller owns budgets and session scheduling.

Only explicit UUID sessions bound to this directory may resume. A completed turn
is reusable locally; a request with an uncertain outcome is never re-submitted.
No credential files are opened or copied. CLI-internal request counts are unknown.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import signal
import stat
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel

from .config import ProviderConfig

MAX_STREAM_BYTES = 8_000_000
MAX_LINE_BYTES = 2_000_000
MAX_RESULT_BYTES = 1_000_000
MAX_STDERR_BYTES = 1_000_000
MAX_PROMPT_BYTES = 2_000_000
MAX_TURNS = 100
VERSION = 1


@dataclass(frozen=True)
class TransportResult:
    session_id: str
    text: str
    turn_id: str
    workdir: str
    event_path: str
    result_path: str
    request_fingerprint: str
    usage: dict[str, int]
    recovered: bool = False


class CodexSessionError(RuntimeError):
    def __init__(self, code, *, session_id=None, turn_id=None, outcome_unknown=True):
        super().__init__(f"Codex session transport: {code}")
        self.code = code
        self.session_id = session_id
        self.turn_id = turn_id
        self.outcome_unknown = outcome_unknown

    @property
    def failure_kind(self):
        if self.code == "format_invalid":
            return "format"
        return "unknown" if self.outcome_unknown else "definitive"


class CodexSessionUnknown(CodexSessionError):
    pass


class CodexSessionFormatError(CodexSessionError):
    pass


class CodexSessionDefinitiveError(CodexSessionError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise CodexSessionError("invalid_session_id", outcome_unknown=False) from None


def _sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(path):
    if not path.is_absolute() or path.resolve() != path:
        raise CodexSessionError("invalid_workdir", outcome_unknown=False)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise CodexSessionError("invalid_workdir", outcome_unknown=False)
    os.chmod(path, 0o700)


def _write_json(path, value):
    """Publish a complete, immutable, private file without replacing history."""
    data = _canonical(value).encode()
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
        _sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_bytes(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise CodexSessionError("artifact_invalid")
        value = source.read(limit + 1)
        if len(value) > limit:
            raise CodexSessionError("artifact_invalid")
        return value


def _read_json(path, limit=MAX_PROMPT_BYTES + 200_000):
    try:
        return json.loads(_read_bytes(path, limit))
    except (OSError, ValueError, TypeError):
        raise CodexSessionError("artifact_invalid") from None


class _Events:
    def __init__(self, expected_session=None):
        self.expected_session = expected_session
        self.session_id = None
        self.started = False
        self.completed = False
        self.text = None
        self.usage = {}

    def consume(self, raw):
        try:
            event = json.loads(raw)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError()
        except (ValueError, TypeError):
            raise CodexSessionError("invalid_event") from None
        kind = event["type"]
        if self.completed:
            raise CodexSessionError("event_after_completion")
        if kind == "thread.started":
            try:
                session_id = _uuid(event.get("thread_id"))
            except CodexSessionError:
                raise CodexSessionError("invalid_thread_id") from None
            if self.session_id is not None or self.expected_session and session_id != self.expected_session:
                raise CodexSessionError("session_id_mismatch")
            self.session_id = session_id
            return session_id
        if kind == "turn.started":
            if self.session_id is None or self.started:
                raise CodexSessionError("invalid_turn_order")
            self.started = True
        elif kind == "turn.completed":
            if not self.started or self.text is None:
                raise CodexSessionError("incomplete_turn")
            self.completed = True
            usage = event.get("usage", {})
            if isinstance(usage, dict):
                self.usage = {key: value for key, value in usage.items() if key in {
                    "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
                } and type(value) is int and value >= 0}
        elif kind in {"turn.failed", "error"}:
            raise CodexSessionError("turn_failed")
        elif kind.startswith("item."):
            item = event.get("item")
            if not self.started or not isinstance(item, dict):
                raise CodexSessionError("invalid_turn_order")
            if item.get("type") in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
                raise CodexSessionError("unexpected_tool_event")
            if kind == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text")
                if not isinstance(text, str) or len(text.encode()) > MAX_RESULT_BYTES:
                    raise CodexSessionError("result_size_limit")
                self.text = text  # Commentary may precede the final assistant message.
        return None


def _parse_log(path, expected_session):
    raw = _read_bytes(path, MAX_STREAM_BYTES)
    if not raw or not raw.endswith(b"\n"):
        raise CodexSessionError("incomplete_event_stream")
    events = _Events(expected_session)
    for line in raw.splitlines():
        if not line or len(line) > MAX_LINE_BYTES:
            raise CodexSessionError("invalid_event")
        events.consume(line)
    if not events.completed:
        raise CodexSessionError("incomplete_turn")
    return events, hashlib.sha256(raw).hexdigest()


def _request_key(request):
    return _hash({key: request[key] for key in ("version", "prompt", "schema", "provider_fingerprint", "session_id")})


def request_fingerprint(config, prompt, schema_type, session_id=None):
    """The caller stores this exact key beside its pre-call database reservation."""
    if session_id is not None:
        _uuid(session_id)
    return _request_key({"version": VERSION, "prompt": prompt, "schema": schema_type.model_json_schema(),
        "provider_fingerprint": _hash(config.model_dump(mode="json")), "session_id": session_id})


def _completed(root, turn, schema_type=None):
    """Read and verify terminal artifacts. Never infer completion from message text."""
    request = _read_json(turn / "request.json")
    key = _request_key(request)
    if turn.name != "turn-" + key or request.get("request_fingerprint") != key:
        raise CodexSessionError("artifact_request_mismatch")
    exit_receipt = _read_json(turn / "exit.json", 10_000)
    if exit_receipt != {"returncode": 0, "request_fingerprint": key}:
        raise CodexSessionError("nonzero_exit")
    events, events_hash = _parse_log(turn / "events.jsonl", request["session_id"])
    binding = _read_json(root / "thread.json", 10_000)
    if binding != {"session_id": events.session_id, "provider_fingerprint": request["provider_fingerprint"]}:
        raise CodexSessionError("session_id_mismatch")
    if schema_type is not None:
        if request["schema"] != schema_type.model_json_schema():
            raise CodexSessionError("schema_mismatch", outcome_unknown=False)
        try:
            schema_type.model_validate_json(events.text)
        except (ValueError, TypeError):
            raise CodexSessionError("format_invalid", outcome_unknown=False) from None
    result = TransportResult(session_id=events.session_id, text=events.text, turn_id=turn.name,
        workdir=str(root), event_path=str(turn / "events.jsonl"), result_path=str(turn / "result.json"),
        request_fingerprint=key, usage=events.usage)
    if (turn / "result.json").exists():
        stored = _read_json(turn / "result.json", MAX_RESULT_BYTES + 20_000)
        if stored != {"result": asdict(result), "events_sha256": events_hash, "schema_sha256": _hash(request["schema"])}:
            raise CodexSessionError("artifact_result_mismatch")
    elif schema_type is None:
        raise CodexSessionError("result_validation_missing")
    return result, events_hash, request


def scan_artifacts(workdir, schema_type: type[BaseModel] | None = None) -> list[TransportResult]:
    """Read completed results; optional schema also permits pre-result crash recovery.

    An invalid/incomplete turn is omitted, never retried. Returned fingerprints
    must be matched against the caller's exact durable call reservation.
    """
    root = Path(workdir)
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        return []
    results = []
    turns = sorted(root.glob("turn-*"))
    if len(turns) > MAX_TURNS:
        return []
    for turn in turns:
        if turn.is_symlink() or not turn.is_dir():
            continue
        try:
            result, _, _ = _completed(root, turn, schema_type)
            results.append(replace(result, recovered=True))
        except (CodexSessionError, OSError, ValueError, TypeError, KeyError):
            continue
    return results


def _argv(config, schema_path, session_id):
    if config.kind != "codex" or not config.command:
        raise CodexSessionError("provider_unsupported", outcome_unknown=False)
    argv = [*config.command, "exec", "--sandbox", "read-only", "--color", "never"]
    if session_id:
        argv += ["resume"]
    argv += ["--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--json",
             "-c", 'approval_policy="never"', "-c", 'sandbox_mode="read-only"',
             "-c", "features.shell_tool=false", "-c", 'web_search="disabled"',
             "--output-schema", str(schema_path)]
    if config.model:
        argv += ["--model", config.model]
    return argv + ([session_id] if session_id else []) + ["-"]


def _environment(config):
    return {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR", "CODEX_HOME",
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", *config.env_allowlist) if key in os.environ}


async def _kill_wait(process):
    # A wrapper can exit while its child still owns pipes/locks in this group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    async def discard(stream):
        while await stream.read(65536):
            pass
    await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())


async def _stream(process, root, turn, request, prompt, on_thread):
    events = _Events(request["session_id"])
    temporary = turn / "events.partial"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    _sync_dir(turn)
    with os.fdopen(descriptor, "wb") as output:
        async def stdout():
            pending = bytearray()
            total = 0
            while chunk := await process.stdout.read(65536):
                total += len(chunk)
                if total > MAX_STREAM_BYTES:
                    raise CodexSessionError("event_stream_limit")
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    if not line or len(line) > MAX_LINE_BYTES:
                        raise CodexSessionError("event_line_limit")
                    output.write(line + b"\n")
                    output.flush()
                    os.fsync(output.fileno())
                    thread_id = events.consume(line)
                    if thread_id:
                        binding = {"session_id": thread_id, "provider_fingerprint": request["provider_fingerprint"]}
                        if (root / "thread.json").exists():
                            if _read_json(root / "thread.json", 10_000) != binding:
                                raise CodexSessionError("session_id_mismatch")
                        else:
                            _write_json(root / "thread.json", binding)
                        if on_thread:
                            try:
                                await on_thread(thread_id)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                raise CodexSessionError("thread_callback_failed") from None
                if len(pending) > MAX_LINE_BYTES:
                    raise CodexSessionError("event_line_limit")
            if pending:
                raise CodexSessionError("incomplete_event_stream")

        async def stderr():
            size = 0
            while chunk := await process.stderr.read(65536):
                size += len(chunk)
                if size > MAX_STDERR_BYTES:
                    raise CodexSessionError("stderr_size_limit")
            # Never persist or expose raw stderr; it can contain account data.

        async def stdin():
            process.stdin.write(prompt.encode())
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            process.stdin.close()

        tasks = [asyncio.create_task(fn()) for fn in (stdout, stderr, stdin)]
        try:
            await asyncio.gather(*tasks)
            await process.wait()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    os.replace(temporary, turn / "events.jsonl")
    _sync_dir(turn)
    _write_json(turn / "exit.json", {"returncode": process.returncode,
                                     "request_fingerprint": request["request_fingerprint"]})


async def run(config: ProviderConfig, workdir, prompt: str, schema_type: type[BaseModel],
              session_id: str | None = None,
              on_thread: Callable[[str], Awaitable[None]] | None = None,
              lock_fd: int | None = None) -> TransportResult:
    """Execute once, or reuse this exact completed request without starting a CLI."""
    if session_id is not None:
        _uuid(session_id)
    if not isinstance(prompt, str) or not prompt or len(prompt.encode()) > MAX_PROMPT_BYTES:
        raise CodexSessionError("prompt_size_limit", outcome_unknown=False)
    if lock_fd is not None:
        if type(lock_fd) is not int or lock_fd < 0:
            raise CodexSessionError("invalid_lock_fd", outcome_unknown=False)
        try:
            os.fstat(lock_fd)
        except OSError:
            raise CodexSessionError("invalid_lock_fd", outcome_unknown=False) from None
    root = Path(workdir)
    _private_directory(root)
    descriptor = os.open(root / ".transport.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CodexSessionError("session_busy", outcome_unknown=False) from None
        return await _run_locked(config, root, prompt, schema_type, session_id, on_thread,
                                 tuple(dict.fromkeys((descriptor,) + ((lock_fd,) if lock_fd is not None else ()))))
    finally:
        os.close(descriptor)


async def _run_locked(config, root, prompt, schema_type, session_id, on_thread, pass_fds):
    request = {"version": VERSION, "prompt": prompt, "schema": schema_type.model_json_schema(),
               "provider_fingerprint": _hash(config.model_dump(mode="json")), "session_id": session_id}
    key = request_fingerprint(config, prompt, schema_type, session_id)
    request["request_fingerprint"] = key
    turn = root / ("turn-" + key)
    if turn.exists():
        if turn.is_symlink():
            raise CodexSessionError("artifact_invalid")
        try:
            result, _, _ = _completed(root, turn, schema_type)
            return replace(result, recovered=True)
        except (CodexSessionError, OSError, ValueError, TypeError, KeyError):
            raise CodexSessionError("previous_outcome_unresolved", session_id=session_id, turn_id=turn.name) from None
    binding = _read_json(root / "thread.json", 10_000) if (root / "thread.json").exists() else None
    if session_id is not None:
        # A fresh batch directory may resume the exact ID supplied by the caller's DB.
        if binding and binding != {"session_id": session_id, "provider_fingerprint": request["provider_fingerprint"]}:
            raise CodexSessionError("session_id_mismatch", outcome_unknown=False)
    elif binding:
        raise CodexSessionError("explicit_resume_required", outcome_unknown=False)
    existing = sorted(root.glob("turn-*"))
    if len(existing) >= MAX_TURNS:
        raise CodexSessionError("turn_limit", outcome_unknown=False)
    for previous in existing:
        try:
            # A malformed but fully completed response may be followed by a format repair.
            old = _read_json(previous / "request.json")
            exit_receipt = _read_json(previous / "exit.json", 10_000)
            events, _ = _parse_log(previous / "events.jsonl", old["session_id"])
            if exit_receipt != {"returncode": 0, "request_fingerprint": old["request_fingerprint"]} or not events.completed:
                raise ValueError()
        except (CodexSessionError, OSError, ValueError, TypeError, KeyError):
            raise CodexSessionError("previous_outcome_unresolved", session_id=session_id) from None
    _private_directory(turn)
    _sync_dir(root)
    if len(_canonical(request).encode()) > MAX_PROMPT_BYTES:
        raise CodexSessionError("prompt_size_limit", outcome_unknown=False)
    _write_json(turn / "request.json", request)
    _write_json(turn / "schema.json", request["schema"])
    process = None
    try:
        argv = _argv(config, turn / "schema.json", session_id)
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=root, env=_environment(config), start_new_session=True, pass_fds=pass_fds))
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            process = await spawning
            raise
        async with asyncio.timeout(config.timeout_seconds):
            await _stream(process, root, turn, request, prompt, on_thread)
        result, events_hash, _ = _completed(root, turn, schema_type)
        _write_json(turn / "result.json", {"result": asdict(result), "events_sha256": events_hash,
                                           "schema_sha256": _hash(request["schema"])})
        return result
    except BaseException as exc:
        if process is not None:
            await _kill_wait(process)
        code = exc.code if isinstance(exc, CodexSessionError) else (
            "timeout_unknown" if isinstance(exc, TimeoutError) else
            "cancelled_unknown" if isinstance(exc, asyncio.CancelledError) else "transport_unknown")
        unknown = not isinstance(exc, CodexSessionError) or exc.outcome_unknown
        binding = _read_json(root / "thread.json", 10_000) if (root / "thread.json").exists() else {}
        recorded_session = binding.get("session_id", session_id)
        _write_json(turn / "failure.json", {"code": code, "outcome_unknown": unknown,
            "session_id": recorded_session, "at": datetime.now(UTC).isoformat()})
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        error_type = (CodexSessionFormatError if code == "format_invalid" else
                      CodexSessionUnknown if unknown else CodexSessionDefinitiveError)
        raise error_type(code, session_id=recorded_session, turn_id=turn.name, outcome_unknown=unknown) from None
