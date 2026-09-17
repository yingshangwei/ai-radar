#!/usr/bin/env python3
"""Publish signed OTA files through the configured Tencent official CLI, without SSH or open ports."""

import argparse
import base64
import concurrent.futures
import hashlib
import importlib.util
import io
import json
import tarfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


tat = module("tat", "tencent-command.py")
publisher = module("publisher", "publish-updates.py")


def command(script, name, receipts):
    content = base64.b64encode(script.encode()).decode()
    if len(content) > 65536:
        raise ValueError("Command exceeds TAT size limit")
    result = tat.call(
        "RunCommand",
        {
            "InstanceIds": ["lhins-e5gcg722"],
            "Content": content,
            "CommandName": name,
            "CommandType": "SHELL",
            "Timeout": 120,
            "SaveCommand": False,
        },
    )
    invocation = result["InvocationId"]
    (receipts / f"{invocation}.json").write_text(json.dumps(result))
    print(f"{name}: {invocation}", flush=True)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        time.sleep(2)
        status = tat.call(
            "DescribeInvocationTasks",
            {
                "Filters": [{"Name": "invocation-id", "Values": [invocation]}],
                "HideOutput": False,
            },
        )
        tasks = status.get("InvocationTaskSet", [])
        if (
            tasks
            and tasks[0]["TaskStatus"] == "SUCCESS"
            and tasks[0]["TaskResult"].get("ExitCode") == 0
        ):
            return invocation
        if tasks and tasks[0]["TaskStatus"] in {
            "SUCCESS",
            "FAILED",
            "TIMEOUT",
            "TERMINATED",
            "CANCELLED",
        }:
            raise RuntimeError(
                f"Remote command failed. Inspect {invocation} with tencent-command.py status; it was not resubmitted."
            )
    raise RuntimeError(
        f"Status deadline exceeded; inspect {invocation} before retrying."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path)
    parser.add_argument("--channel", choices=["preview", "stable"], default="preview")
    args = parser.parse_args()
    certificate = ROOT / "app/updates/certificate.pem"
    publisher.verify(args.stage, certificate)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for file in sorted(args.stage.rglob("*")):
            if file.is_file():
                tar.add(
                    file, arcname=str(file.relative_to(args.stage)), recursive=False
                )
    data = archive.getvalue()
    upload = uuid.uuid4().hex
    receipts = ROOT / "dist/ota" / ("upload-" + upload)
    receipts.mkdir(parents=True)
    # Preserve exact gzip bytes so interrupted uploads can resume missing parts.
    # Recreating tar.gz later changes its header timestamp and whole-file hash.
    archive_path = receipts / "archive.tar.gz"
    archive_path.write_bytes(data)
    archive_path.chmod(0o600)
    remote = "/tmp/radar-ota-upload-" + upload
    parts = [data[i : i + 30000] for i in range(0, len(data), 30000)]
    (receipts / "upload.json").write_text(
        json.dumps(
            {
                "remote": remote,
                "parts": len(parts),
                "sha256": hashlib.sha256(data).hexdigest(),
                "channel": args.channel,
            }
        )
    )

    def send(index):
        part = base64.b64encode(parts[index]).decode()
        script = f"set -e\npython3 - <<'PYREMOTE'\nfrom pathlib import Path\nimport base64\np=Path({remote!r});p.mkdir(exist_ok=True)\nwith (p / 'part-{index:04}').open('xb') as f: f.write(base64.b64decode({part!r}))\nPYREMOTE\n"
        return command(script, f"radar-ota-part-{index}", receipts)

    # Every worker writes a distinct chunk; no activation happens until every result is inspected.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(send, i) for i in range(len(parts))]
        failures = []
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except (Exception, SystemExit) as error:  # noqa: BLE001 - inspect every worker before activation
                failures.append(error)
        if failures:
            raise RuntimeError(
                f"{len(failures)} chunk operations failed; inspect receipts in {receipts}. No activation attempted."
            ) from failures[0]
    script = f"""set -e
python3 - <<'PYREMOTE'
from pathlib import Path
import hashlib,io,tarfile
p=Path({remote!r})
data=b''.join((p/f'part-{{i:04}}').read_bytes() for i in range({len(parts)}))
assert hashlib.sha256(data).hexdigest()=={hashlib.sha256(data).hexdigest()!r}, 'Transfer integrity failed'
assert hashlib.sha256(Path('/opt/ai-radar/updates/certificate.pem').read_bytes()).hexdigest()=={hashlib.sha256(certificate.read_bytes()).hexdigest()!r}, 'Server certificate differs'
stage=p/'stage';stage.mkdir()
with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as tar:
 members=tar.getmembers()
 assert sum(m.size for m in members)<100_000_000
 for member in members:
  dest=stage/member.name
  assert member.isfile() and not Path(member.name).is_absolute() and dest.resolve().is_relative_to(stage.resolve())
  dest.parent.mkdir(parents=True,exist_ok=True)
  with dest.open('xb') as f: f.write(tar.extractfile(member).read())
PYREMOTE
python3 /opt/ai-radar/updates/publish-updates.py {remote}/stage --certificate /opt/ai-radar/updates/certificate.pem --destination /var/lib/ai-radar-updates --channel {args.channel}
"""
    invocation = command(script, "radar-ota-activate", receipts)
    print(
        json.dumps(
            {
                "activated": True,
                "channel": args.channel,
                "invocation": invocation,
                "receipts": str(receipts),
            }
        )
    )


if __name__ == "__main__":
    main()
