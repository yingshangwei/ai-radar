#!/usr/bin/env python3
"""Submit or inspect one Tencent TAT command through the user's configured official CLI.

No credentials are loaded, printed or embedded by this helper. Submission is never
retried automatically: inspect the returned invocation before deciding next steps.
"""

import argparse
import base64
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def call(action, payload):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as source:
        json.dump(payload, source)
        source.flush()
        result = subprocess.run(
            [
                str(ROOT / "server/.venv/bin/tccli"),
                "tat",
                action,
                "--region",
                "ap-seoul",
                "--cli-input-json",
                "file://" + source.name,
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    if result.returncode:
        raise SystemExit(result.stderr or result.stdout or "Tencent CLI request failed")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("script", type=Path)
    run.add_argument("--name", required=True)
    run.add_argument("--timeout", type=int, default=60)
    status = sub.add_parser("status")
    status.add_argument("invocation")
    status.add_argument("--output-limit", type=int, default=16000)
    args = parser.parse_args()
    if args.action == "run":
        content = base64.b64encode(args.script.read_bytes()).decode()
        if len(content) > 65536:
            parser.error("TAT command exceeds the official 64 KiB encoded limit")
        result = call(
            "RunCommand",
            {
                "InstanceIds": ["lhins-e5gcg722"],
                "Content": content,
                "CommandName": args.name,
                "CommandType": "SHELL",
                "Timeout": args.timeout,
                "SaveCommand": False,
            },
        )
        print(json.dumps(result, ensure_ascii=False))
        folder = ROOT / "dist/cloud"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (result["InvocationId"] + ".json")).write_text(json.dumps(result))
    else:
        result = call(
            "DescribeInvocationTasks",
            {
                "Filters": [{"Name": "invocation-id", "Values": [args.invocation]}],
                "HideOutput": False,
            },
        )
        for task in result.get("InvocationTaskSet", []):
            output = task.get("TaskResult", {})
            print(
                json.dumps(
                    {
                        "invocation": task["InvocationId"],
                        "task": task["InvocationTaskId"],
                        "status": task["TaskStatus"],
                        "exit_code": output.get("ExitCode"),
                        "dropped_bytes": output.get("Dropped"),
                        "error": task.get("ErrorInfo"),
                    },
                    ensure_ascii=False,
                )
            )
            print(
                base64.b64decode(output.get("Output") or "").decode(errors="replace")[
                    -args.output_limit :
                ]
            )
        if not result.get("InvocationTaskSet"):
            print(
                "No task is visible yet; recheck this invocation before resubmitting."
            )


if __name__ == "__main__":
    main()
