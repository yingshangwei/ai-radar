"""Read actual public pages through the local worker without article or model writes.

Supply RADAR_BROWSER_WORKER_TOKEN in the environment. Reports include source
excerpts and should be kept private; tokens and cookies are never recorded.
"""

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= len(args.url) <= 10:
        parser.error("Supply between 1 and 10 public pages")
    for url in args.url:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            parser.error("Only public HTTP(S) URLs without credentials are supported")
    token = os.environ.get("RADAR_BROWSER_WORKER_TOKEN", "")
    if not token:
        parser.error("RADAR_BROWSER_WORKER_TOKEN is required")
    if args.output.exists():
        parser.error("Output already exists; retain prior results and choose another path")
    with httpx.Client(base_url="http://127.0.0.1:18475", timeout=90, trust_env=False,
                      headers={"Authorization": "Bearer " + token}) as client:
        health = client.get("/health")
        health.raise_for_status()
        if health.json().get("interactive", True):
            parser.error("A user authorization window is active; leave it undisturbed")
        report = {"started_at": datetime.now(UTC).isoformat(), "worker": health.json(),
                  "article_writes": 0, "model_calls": 0, "results": []}
        with args.output.open("x", encoding="utf-8") as output:
            os.chmod(args.output, 0o600)
            for url in args.url:
                start = time.monotonic()
                try:
                    response = client.post("/fetch", json={"url": url})
                    response.raise_for_status()
                    result = response.json()
                    doc = result.get("document", {})
                    text = doc.get("text", "")
                    entry = {"url": url, "status": result.get("status"), "final_url": result.get("url"),
                             "title": doc.get("title", ""), "characters": len(text),
                             "engine": result.get("extraction_engine", doc.get("extraction_engine", "")),
                             "version": doc.get("extraction_version", ""),
                             "fallback_reason": doc.get("extraction_fallback_reason", ""),
                             "partial": doc.get("partial"), "readiness_timed_out": result.get("readiness_timed_out"),
                             "links": len(doc.get("links", [])), "message": result.get("message", ""),
                             "text_sha256": hashlib.sha256(text.encode()).hexdigest() if text else "",
                             "head": text[:800], "tail": text[-400:],
                             "headings": [line[:180] for line in text.splitlines() if line.startswith("#")][:12],
                             "table_rows": sum(line.lstrip().startswith("|") for line in text.splitlines()),
                             "code_lines": sum(line.startswith(("    ", "```")) for line in text.splitlines())}
                except (httpx.HTTPError, ValueError) as exc:
                    entry = {"url": url, "status": "diagnostic_failed", "error_type": type(exc).__name__}
                entry["seconds"] = round(time.monotonic() - start, 2)
                report["results"].append(entry)
                output.seek(0)
                json.dump(report, output, ensure_ascii=False, indent=2)
                output.truncate()
                output.flush()
                print(json.dumps({key: value for key, value in entry.items()
                                  if key not in {"head", "tail", "headings"}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
