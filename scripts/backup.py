#!/usr/bin/env python3
"""Consistent SQLite backup including WAL; run as the service account, not by copying a live DB."""
import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("database", type=Path)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
if not args.database.is_file():
    parser.error("database does not exist")
os.umask(0o077)
args.destination.mkdir(parents=True, exist_ok=True)
target = args.destination / f"radar-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
with sqlite3.connect(f"file:{args.database}?mode=ro", uri=True) as source, sqlite3.connect(target) as dest:
    source.backup(dest)
print(target)
