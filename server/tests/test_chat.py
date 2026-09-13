import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radar.api import create_app
from radar.chat import ChatError
from radar.chat_models import ChatSession, ChatTurn
from radar.config import Settings

SCRIPT = """
import json,sys,uuid
from pathlib import Path
args=sys.argv[1:]; prompt=sys.stdin.read()
thread=args[-2] if 'resume' in args else str(uuid.uuid4())
Path('invocation.json').write_text(json.dumps({'args':args,'prompt':prompt}))
def send(x):print(json.dumps(x),flush=True)
send({'type':'thread.started','thread_id':thread})
send({'type':'turn.started'})
send({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'answer':'本轮快照显示服务状态，资料范围有限。','citations':[]})}})
send({'type':'turn.completed','usage':{'input_tokens':20,'output_tokens':12}})
"""


@pytest.fixture
def app(tmp_path):
    script = tmp_path / "fake.py"
    script.write_text(SCRIPT)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'anthropic_news_enabled=false\n[provider]\nkind="codex"\ncommand='
        + json.dumps([sys.executable, str(script)])
        + "\n[chat]\nenabled=true\nstate_directory="
        + json.dumps(str(tmp_path / "chat"))
        + "\n[industry]\nenabled=false\n"
    )
    a = create_app(
        Settings(
            config_path=str(cfg),
            database_url=f"sqlite:///{tmp_path}/db",
            reader_token="reader",
            scheduler_enabled=False,
            usage_database_path="",
            accounts_config_path="",
        )
    )
    yield a
    with a.state.sessions() as s:
        s.bind.dispose()


def setup_session(app):
    c = TestClient(app)
    c.headers["Authorization"] = "Bearer reader"
    return c, c.post("/v1/chat/sessions").json()["id"]


async def test_real_transport_new_resume_and_model_switch(app):
    c, sid = setup_session(app)
    for i, profile in enumerate(["standard", "standard", "confirmation"]):
        assert (
            c.post(
                f"/v1/chat/sessions/{sid}/messages",
                json={"id": f"message_test_{i}", "question": "服务状态怎样？", "profile": profile},
            ).status_code
            == 200
        )
        assert await app.state.chat.run_one()
        with app.state.sessions() as db:
            session = db.get(ChatSession, sid)
            turn = db.get(ChatTurn, f"message_test_{i}")
            assert turn.status == "completed"
            if i == 0:
                first = session.cli_session
            if i == 1:
                assert session.cli_session == first
            if i == 2:
                assert session.cli_session != first
            invocation = json.loads((Path(turn.transport["root"]) / "invocation.json").read_text())
            assert ("resume" in invocation["args"]) == (i == 1)
            assert "features.shell_tool=false" in invocation["args"]
            assert 'web_search="disabled"' in invocation["args"]
            assert "Bearer reader" not in invocation["prompt"]
            if i == 2:
                assert "本轮快照显示服务状态" in invocation["prompt"]
    detail = c.get(f"/v1/chat/sessions/{sid}").json()
    assert len(detail["messages"]) == 3
    assert all(m["tokens"]["input_tokens"] == 20 for m in detail["messages"])
    assert "transport" not in detail["messages"][0]


async def test_idempotency_auth_limits_and_fifo(app):
    c, sid = setup_session(app)
    payload = {"id": "same_message_123", "question": "检查任务"}
    for _ in range(2):
        assert c.post(f"/v1/chat/sessions/{sid}/messages", json=payload).status_code == 200
    assert (
        c.post(f"/v1/chat/sessions/{sid}/messages", json={**payload, "question": "different"}).status_code
        == 409
    )
    assert (
        c.post(
            f"/v1/chat/sessions/{sid}/messages",
            json={"id": "bad_profile_123", "question": "test", "profile": "arbitrary"},
        ).status_code
        == 422
    )
    assert TestClient(app).get("/v1/chat").status_code == 401
    assert TestClient(app).post(f"/v1/chat/sessions/{sid}/messages", json=payload).status_code == 401
    app.state.chat.options.max_pending = 1
    with pytest.raises(ChatError):
        app.state.chat.enqueue(sid, "next_message_123", "next", "standard")
    assert await app.state.chat.run_one()
    assert not await app.state.chat.run_one()
    assert len(c.get(f"/v1/chat/sessions/{sid}").json()["messages"]) == 1


async def test_crash_receipt_recovers_without_second_call(app, monkeypatch):
    _, sid = setup_session(app)
    app.state.chat.enqueue(sid, "recover_message_123", "status", "standard")
    await app.state.chat.run_one()
    with app.state.chat.transaction() as db:
        row = db.get(ChatTurn, "recover_message_123")
        row.status = "running"
        row.answer = ""
    monkeypatch.setattr("radar.codex_sessions.run", lambda *a, **k: pytest.fail("must not replay"))
    app.state.chat.recover()
    with app.state.sessions() as db:
        assert db.get(ChatTurn, "recover_message_123").status == "completed"
    assert not await app.state.chat.run_one()


async def test_unknown_and_expired_never_replayed(app, monkeypatch):
    _, sid = setup_session(app)
    app.state.chat.enqueue(sid, "unknown_message_123", "status", "standard")
    with app.state.chat.transaction() as db:
        db.get(ChatTurn, "unknown_message_123").status = "running"
    monkeypatch.setattr("radar.codex_sessions.run", lambda *a, **k: pytest.fail("must not call"))
    app.state.chat.recover()
    with app.state.sessions() as db:
        assert db.get(ChatTurn, "unknown_message_123").status == "outcome_unknown"
    app.state.chat.enqueue(sid, "expired_message_123", "status", "standard")
    with app.state.chat.transaction() as db:
        db.get(ChatTurn, "expired_message_123").created_at = (
            datetime.now(UTC) - timedelta(hours=2)
        ).isoformat()
    assert await app.state.chat.run_one()
    with app.state.sessions() as db:
        assert db.get(ChatTurn, "expired_message_123").status == "expired"


def test_snapshot_public_fields_and_migration(app):
    snap = app.state.chat.snapshot("检查服务状态")
    assert snap["status"]["scheduler_enabled"] is False
    assert snap["status"]["queue"] is not None
    assert "reader" not in json.dumps(snap)
    assert "provider" not in snap["status"]
    with app.state.sessions() as db:
        assert not list(db.scalars(select(ChatTurn)))
        assert not list(db.scalars(select(ChatSession)))
