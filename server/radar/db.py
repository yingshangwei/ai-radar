import json
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker

from .models import Base, Watch

WATCHLIST = [
    ("OpenAI", "OpenAI", "OpenAI", "官方动态"),
    ("Anthropic", "AnthropicAI", "Anthropic", "Claude · 官方动态"),
    ("Google DeepMind", "GoogleDeepMind", "Google", "研究与产品"),
    ("Google AI", "GoogleAI", "Google", "官方动态"),
    ("Meta AI", "AIatMeta", "Meta", "开源与研究"),
    ("Sam Altman", "sama", "OpenAI", "重点人物"),
    ("Greg Brockman", "gdb", "OpenAI", "重点人物"),
    ("Kevin Weil", "kevinweil", "OpenAI", "重点人物"),
    ("Jakub Pachocki", "merettm", "OpenAI", "技术专家"),
    ("Dario Amodei", "DarioAmodei", "Anthropic", "重点人物"),
    ("Boris Cherny", "bcherny", "Anthropic", "Claude Code"),
    ("Demis Hassabis", "demishassabis", "Google", "重点人物"),
    ("Jeff Dean", "JeffDean", "Google", "技术专家"),
    ("Sundar Pichai", "sundarpichai", "Google", "重点人物"),
    ("Andrej Karpathy", "karpathy", "Independent", "技术与思想"),
    ("Tibo", "tibo_maker", "Independent", "AI 产品与独立开发"),
    ("Hugging Face", "huggingface", "Hugging Face", "开源生态"),
]


JOB_CONTROL_COLUMNS = {
    "queued_at": "VARCHAR(40)", "run_started_at": "VARCHAR(40)", "request_day": "VARCHAR(10)",
    "force": "BOOLEAN NOT NULL DEFAULT 0", "attempt": "INTEGER NOT NULL DEFAULT 0",
    "max_attempts": "INTEGER NOT NULL DEFAULT 3", "retry_at": "VARCHAR(40)",
    "heartbeat_at": "VARCHAR(40)", "progress_at": "VARCHAR(40)",
    "phase": "VARCHAR(30) NOT NULL DEFAULT ''", "reason_code": "VARCHAR(40) NOT NULL DEFAULT ''",
    "owner": "VARCHAR(100) NOT NULL DEFAULT ''", "more_pending": "BOOLEAN NOT NULL DEFAULT 0",
}


def migrate_job_controls(engine):
    """Add only queue controls; retain every preexisting job and article field."""
    if engine.dialect.name != "sqlite":
        return  # SQLite is the supported deployment; other engines use their migration tools.
    with engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        if not inspect(connection).has_table("jobs"):
            return
        columns = {item["name"] for item in inspect(connection).get_columns("jobs")}
        for name, declaration in JOB_CONTROL_COLUMNS.items():
            if name not in columns:
                connection.exec_driver_sql(f'ALTER TABLE jobs ADD COLUMN "{name}" {declaration}')


def database(url: str):
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {}
    engine = create_engine(url, json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False), **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")

    migrate_job_controls(engine)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for name, handle, org, role in WATCHLIST:
            key = f"x:{handle.lower()}"
            if not session.get(Watch, key):
                session.add(Watch(id=key, name=name, handle=handle, organization=org, role=role))
    return engine, sessions
