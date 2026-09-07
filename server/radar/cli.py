import argparse
import asyncio
import json
import os
import secrets
from datetime import date
from pathlib import Path

from .config import Settings
from .db import database
from .models import Job
from .pipeline import Pipeline, as_dict, ingest
from .schemas import ImportBatch


def main():
    parser = argparse.ArgumentParser(description="AI Radar server operations")
    parser.add_argument("action", choices=["init", "collect", "digest", "daily", "import", "translate", "read"])
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--force", action="store_true")
    recheck = parser.add_mutually_exclusive_group()
    recheck.add_argument("--recheck-article", action="append", default=[], metavar="ARTICLE_ID")
    recheck.add_argument("--recheck-editorial", action="store_true")
    args = parser.parse_args()
    rechecking = bool(args.recheck_article or args.recheck_editorial)
    if rechecking and args.action != "translate":
        parser.error("翻译复核选项只能用于 translate")
    if rechecking and (args.file or args.date):
        parser.error("翻译复核只接受已有文章 ID 或历史范围，不能使用 --file 或 --date")
    if args.action == "init":
        target = Path(".env")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(
                f"RADAR_READER_TOKEN={secrets.token_urlsafe(32)}\nRADAR_ADMIN_TOKEN={secrets.token_urlsafe(32)}\n"
            )
        print("Created .env with owner-only permissions; tokens are not printed.")
        return
    settings = Settings()
    config = settings.load()
    engine, sessions = database(settings.database_url)
    try:
        if rechecking:
            from .translation_recheck import recheck_translations

            result = asyncio.run(recheck_translations(
                sessions, config.translation, article_ids=args.recheck_article,
                editorial=args.recheck_editorial, force=args.force,
            ))
            print(json.dumps(result, ensure_ascii=False))
            if result["status"] != "completed":
                raise SystemExit(1)
        elif args.action == "import":
            if not args.file:
                parser.error("--file is required")
            batch = ImportBatch.model_validate_json(args.file.read_text())
            with sessions.begin() as session:
                print(json.dumps({"accepted": ingest(session, batch.articles, config)}))
            if config.translation.enabled or config.reading.enabled:
                pipeline = Pipeline(sessions, config)
                uid = asyncio.run(pipeline.run("read" if config.reading.enabled else "translate"))
                with sessions() as session:
                    print(json.dumps(as_dict(session.get(Job, uid)), ensure_ascii=False))
        else:
            pipeline = Pipeline(sessions, config)
            uid = asyncio.run(pipeline.run(args.action, args.date, args.force))
            with sessions() as session:
                job = session.get(Job, uid)
                print(json.dumps(as_dict(job), ensure_ascii=False))
                if job.status != "completed":
                    raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
