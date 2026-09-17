"""Run user chat independently so restarting the API does not kill a repair."""
import asyncio
import fcntl
import json
import os
from pathlib import Path

from .chat import ChatService
from .config import Settings
from .db import database
from .models import now_iso
from .pipeline import Pipeline


async def serve():
    settings = Settings()
    config = settings.load()
    if not config.chat.enabled or not config.chat.external_worker:
        raise RuntimeError("Enable chat.external_worker before starting the dedicated worker")
    root = Path(config.chat.state_directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        engine, sessions = database(settings.database_url)
        service = ChatService(Pipeline(sessions, config), settings)
        await service.start(dedicated=True)
        try:
            while service.task and not service.task.done():
                temporary = root / "heartbeat.tmp"
                temporary.write_text(json.dumps({"at": now_iso(), "pid": os.getpid()}))
                temporary.replace(root / "heartbeat.json")
                await asyncio.sleep(10)
            if service.task:
                await service.task  # A failed worker must fail systemd, not idle forever.
        finally:
            await service.stop()
            engine.dispose()


if __name__ == "__main__":
    asyncio.run(serve())
