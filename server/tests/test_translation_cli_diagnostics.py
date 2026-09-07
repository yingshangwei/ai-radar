import json
import logging
import sys

import pytest

from radar import cli, translation_recheck
from radar.config import RadarConfig, TranslationConfig
from radar.models import Job
from radar.translation import TranslationService

PRIVATE = "private-article-and-candidate-never-log"
KEY = "private-api-key-never-log"
PRIVATE_URL = "https://private.example/article?token=never-log"


def logger_state(logger):
    return logger.level, logger.propagate, logger.disabled, logger.handlers


@pytest.fixture
def logging_state():
    loggers = [logging.getLogger(name) for name in ("", "httpx", "openai", "radar.translation")]
    original = [logger_state(logger) for logger in loggers]
    for logger in loggers[1:]:
        logger.setLevel(logging.WARNING)
    translation = loggers[-1]
    translation.handlers = [logging.NullHandler()]
    translation.propagate = False
    translation.disabled = False
    before = [logger_state(logger) for logger in loggers]
    yield loggers, before
    for logger, state in zip(loggers, original, strict=True):
        logger.setLevel(state[0])
        logger.propagate, logger.disabled, logger.handlers = state[1:]


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    config = RadarConfig(translation=TranslationConfig(enabled=True))

    class Configured:
        database_url = f"sqlite:///{tmp_path}/diagnostics.db"

        def load(self):
            return config

    monkeypatch.setattr(cli, "Settings", Configured)
    return config


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mode", ["limited", "recheck", "ordinary"])
def test_cli_diagnostics_are_opt_in_safe_stderr_and_restore_after_repeated_runs(
    configuration, logging_state, monkeypatch, respx_mock, capsys, enabled, mode,
):
    route = respx_mock.post("https://api.deepseek.com/chat/completions").respond(200, json={
        "id": "test", "object": "chat.completion", "created": 0, "model": KEY,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({"private": PRIVATE}),
            "reasoning_content": KEY + PRIVATE_URL,
        }}],
        "usage": {"prompt_tokens": 17, "completion_tokens": 31, "total_tokens": 48,
                  "completion_tokens_details": {"reasoning_tokens": 23}},
    })

    async def operation(sessions, config, **kwargs):
        await TranslationService(sessions, config)._completion(
            {"untrusted_parts": [{"id": "body-0", "source": PRIVATE_URL, "candidate": PRIVATE}]},
            system=PRIVATE, model=PRIVATE_URL, stage="audit",
        )
        logging.getLogger("httpx").info(KEY)
        logging.getLogger("openai").info(KEY)
        return {"status": "completed", "limit": 1}

    class OperationPipeline:
        def __init__(self, sessions, config):
            self.sessions, self.config = sessions, config

        async def run(self, action, day, force):
            result = await operation(self.sessions, self.config.translation)
            with self.sessions.begin() as session:
                job = Job(kind=action, status=result["status"])
                session.add(job)
                session.flush()
                return job.id

    monkeypatch.setattr(translation_recheck, "translate_limited", operation)
    monkeypatch.setattr(translation_recheck, "recheck_translations", operation)
    monkeypatch.setattr(cli, "Pipeline", OperationPipeline)
    options = {"limited": ["--limit", "1"], "recheck": ["--recheck-editorial"], "ordinary": []}[mode]
    monkeypatch.setattr(sys, "argv", ["radar", "translate", *options,
                                      *(["--translation-diagnostics"] if enabled else [])])
    loggers, before = logging_state
    for _ in range(2):
        cli.main()
        captured = capsys.readouterr()
        assert json.loads(captured.out)["status"] == "completed"
        assert [logger_state(logger) for logger in loggers] == before
        assert loggers[-1].handlers is before[-1][3]
        assert all(value not in captured.out + captured.err for value in (PRIVATE, KEY, PRIVATE_URL))
        if enabled:
            lines = captured.err.splitlines()
            assert len(lines) == 1 and lines[0].startswith("translation_completion ")
            record = json.loads(lines[0].removeprefix("translation_completion "))
            assert set(record) == {"stage", "model", "elapsed_ms", "finish_reason",
                                   "input_tokens", "output_tokens", "reasoning_tokens"}
            assert record["stage"] == "audit" and record["model"].startswith("sha256:")
            assert record["finish_reason"] == "stop" and type(record["elapsed_ms"]) is int
            assert [record[key] for key in ("input_tokens", "output_tokens", "reasoning_tokens")] == [17, 31, 23]
        else:
            assert captured.err == ""
    assert route.call_count == 2


@pytest.mark.parametrize("action", ["init", "collect", "digest", "daily", "import", "read"])
def test_diagnostics_reject_non_translation_actions_before_loading_configuration(
    monkeypatch, logging_state, action,
):
    monkeypatch.setattr(sys, "argv", ["radar", action, "--translation-diagnostics"])
    monkeypatch.setattr(cli, "Settings", lambda: pytest.fail("Invalid flags must not load configuration"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    loggers, before = logging_state
    assert [logger_state(logger) for logger in loggers] == before


@pytest.mark.parametrize("failure", ["failed_job", "exception", "configuration"])
def test_diagnostics_restore_logger_after_failure(
    configuration, logging_state, monkeypatch, capsys, failure,
):
    async def operation(*args, **kwargs):
        if failure == "exception":
            raise RuntimeError("synthetic operation failure")
        return {"status": "failed"}

    def invalid_settings():
        raise RuntimeError("synthetic configuration failure")

    monkeypatch.setattr(translation_recheck, "translate_limited", operation)
    if failure == "configuration":
        monkeypatch.setattr(cli, "Settings", invalid_settings)
    monkeypatch.setattr(sys, "argv", ["radar", "translate", "--limit", "1", "--translation-diagnostics"])
    with pytest.raises(SystemExit if failure == "failed_job" else RuntimeError):
        cli.main()
    loggers, before = logging_state
    assert [logger_state(logger) for logger in loggers] == before
    assert loggers[-1].handlers is before[-1][3]
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"status": "failed"} if failure == "failed_job" else captured.out == ""
