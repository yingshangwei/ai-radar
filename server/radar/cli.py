import argparse
import asyncio
import json
import logging
import os
import secrets
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from .config import Settings
from .db import database
from .models import Job
from .pipeline import Pipeline, as_dict, ingest
from .schemas import ImportBatch


@contextmanager
def _translation_diagnostic_logging(enabled):
    if not enabled:
        yield
        return
    logger = logging.getLogger("radar.translation")
    previous = logger.level, logger.propagate, logger.disabled, logger.handlers
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.disabled = False
    try:
        yield
    finally:
        logger.setLevel(previous[0])
        logger.propagate, logger.disabled, logger.handlers = previous[1:]
        handler.close()


def main():
    parser = argparse.ArgumentParser(description="AI Radar server operations")
    parser.add_argument("action", choices=["init", "collect", "digest", "daily", "import", "translate", "read"])
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, metavar="N", help="Limit a translate batch to 1..500 caches")
    parser.add_argument("--errors-only", action="store_true", help="Resume only existing error caches with --limit")
    parser.add_argument("--revalidate-machine", action="store_true",
                        help="Recheck machine rules using saved model approvals, without calling a model")
    parser.add_argument("--translation-diagnostics", action="store_true",
                        help="Write safe translation completion diagnostics to stderr")
    recheck = parser.add_mutually_exclusive_group()
    recheck.add_argument("--recheck-article", action="append", default=[], metavar="ARTICLE_ID")
    recheck.add_argument("--recheck-editorial", action="store_true")
    args = parser.parse_args()
    if args.translation_diagnostics and args.action != "translate":
        parser.error("--translation-diagnostics 只能用于 translate")
    rechecking = bool(args.recheck_article or args.recheck_editorial)
    if args.revalidate_machine and (
        args.action != "translate" or args.limit is None or args.force or args.errors_only
        or rechecking or args.file or args.date
    ):
        parser.error("--revalidate-machine 必须与 translate --limit 合用，不能与 --force、--errors-only、复核、--file 或 --date 合用")
    if rechecking and args.action != "translate":
        parser.error("翻译复核选项只能用于 translate")
    if rechecking and (args.file or args.date):
        parser.error("翻译复核只接受已有文章 ID 或历史范围，不能使用 --file 或 --date")
    if args.limit is not None:
        if not 1 <= args.limit <= 500:
            parser.error("--limit 必须是 1 到 500 之间的整数")
        if args.action != "translate" or rechecking or args.file or args.date:
            parser.error("--limit 只能用于普通 translate，不能与复核、--file 或 --date 合用")
    if args.errors_only and (args.action != "translate" or args.limit is None or rechecking or args.file or args.date):
        parser.error("--errors-only 必须与普通 translate --limit 合用，不能使用复核、--file 或 --date")
    if args.action == "init":
        target = Path(".env")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(
                f"RADAR_READER_TOKEN={secrets.token_urlsafe(32)}\nRADAR_ADMIN_TOKEN={secrets.token_urlsafe(32)}\n"
            )
        print("Created .env with owner-only permissions; tokens are not printed.")
        return
    with _translation_diagnostic_logging(args.translation_diagnostics):
        _run(args, parser, rechecking)


def _run(args, parser, rechecking):
    settings = Settings()
    config = settings.load()
    engine, sessions = database(settings.database_url)
    try:
        if args.limit is not None:
            from .translation_recheck import translate_limited

            result = asyncio.run(translate_limited(
                sessions, config.translation, limit=args.limit, force=args.force, errors_only=args.errors_only,
                **({"machine_only": True} if args.revalidate_machine else {}),
            ))
            print(json.dumps(result, ensure_ascii=False))
            if result["status"] != "completed":
                raise SystemExit(1)
        elif rechecking:
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
