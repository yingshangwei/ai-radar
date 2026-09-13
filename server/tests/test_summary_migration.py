"""Adding private review state preserves the existing SQLite schema and rows."""

import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from radar.db import WATCHLIST, database
from radar.models import Base, Digest, SummaryReview, Watch

OLD_TABLES = {
    "articles", "article_translations", "translations", "document_analyses", "web_documents",
    "article_documents", "website_access", "sources", "article_reading", "document_captures",
    "digests", "watches", "jobs", "translation_account_states", "x_collection_states",
}


def snapshot(path):
    with sqlite3.connect(path) as connection:
        return {name: {
            "schema": connection.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? ORDER BY type,name", (name,),
            ).fetchall(),
            "rows": connection.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall(),
        } for name in sorted(OLD_TABLES)}


def test_add_review_table_and_restart_preserve_old_published_data(tmp_path):
    path = tmp_path / "existing.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in OLD_TABLES])
    with Session(engine) as session, session.begin():
        for name, handle, org, role in WATCHLIST:
            session.add(Watch(id=f"x:{handle.lower()}", name=name, handle=handle, organization=org, role=role))
        session.add(Digest(date="2020-09-08", title="已经发布", overview="已保存的历史日报。",
                           stories=[], provider="no_updates", source_count=0, coverage=[],
                           window_start="2020-09-07T00:00:00+00:00", window_end="2020-09-08T00:00:00+00:00"))
    engine.dispose()
    before = snapshot(path)
    for _ in range(2):
        upgraded, sessions = database(url)
        try:
            assert snapshot(path) == before
            with sessions() as session:
                assert session.query(SummaryReview).count() == 0
            with sqlite3.connect(path) as connection:
                actual = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
                )}
                discovery = {"discovery_candidates", "discovery_calls", "discovery_entities", "discovery_watches"}
                agent_tables = {"agent_sessions", "agent_batches"}
                industry = {"industry_evidence", "industry_tracking", "industry_assessments"}
                chat = {"chat_sessions", "chat_turns"}
                assert actual == OLD_TABLES | {"summary_reviews"} | discovery | agent_tables | industry | chat
                for table in chat:
                    assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
                for table in agent_tables:
                    assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
                for table in discovery:
                    assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
                for table in industry:
                    assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        finally:
            upgraded.dispose()
