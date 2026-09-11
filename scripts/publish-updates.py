#!/usr/bin/env python3
"""Verify an offline-signed publication, then atomically activate one channel.

Run locally for validation or as root on the update host. Never accepts a private key.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

RUNTIME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
ASSET = re.compile(
    r"([0-9a-f]{64})\.(bundle|png|jpg|jpeg|webp|gif|ttf|otf|woff|woff2|bin)\Z"
)
ORIGIN = "https://radar.yswdra.cn/updates/assets/"


def verify(stage: Path, certificate: Path):
    if any(p.is_symlink() for p in stage.rglob("*")):
        raise ValueError("Symlinks are forbidden")
    pointers = list((stage / "channels").glob("*/*/*.json"))
    if len(pointers) != 1:
        raise ValueError("Exactly one channel/platform/runtime per publication")
    pointer = pointers[0]
    channel, platform, filename = pointer.relative_to(stage / "channels").parts
    runtime = filename.removesuffix(".json")
    if (
        channel not in {"preview", "stable"}
        or platform not in {"android", "ios"}
        or not RUNTIME.fullmatch(runtime)
    ):
        raise ValueError("Invalid target")
    record = json.loads(pointer.read_bytes())
    release = str(uuid.UUID(record["release"]))
    kind = record["kind"]
    if kind not in {"manifest", "rollback"}:
        raise ValueError("Invalid publication kind")
    body_path = stage / "releases" / release / f"{platform}.{kind}.json"
    sig_path = body_path.with_suffix(".sig")
    body = body_path.read_bytes()
    if len(body) > 256_000:
        raise ValueError("Manifest exceeds limit")
    signature = base64.b64decode(sig_path.read_bytes(), validate=True)
    with tempfile.TemporaryDirectory() as tmp:
        public = Path(tmp) / "public.pem"
        sig = Path(tmp) / "signature.bin"
        subprocess.run(
            ["openssl", "x509", "-in", str(certificate), "-checkend", "0", "-noout"],
            check=True,
            capture_output=True,
        )
        public.write_bytes(
            subprocess.check_output(
                ["openssl", "x509", "-in", str(certificate), "-pubkey", "-noout"]
            )
        )
        sig.write_bytes(signature)
        subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                str(public),
                "-signature",
                str(sig),
                str(body_path),
            ],
            check=True,
            capture_output=True,
        )
    data = json.loads(body)
    allowed = {pointer, body_path, sig_path}
    if kind == "manifest":
        uuid.UUID(data["id"])
        if (
            data["runtimeVersion"] != runtime
            or data["metadata"]["platform"] != platform
        ):
            raise ValueError("Manifest does not match target")
        timestamp = data["createdAt"]
        for asset in [data["launchAsset"], *data["assets"]]:
            if not asset["url"].startswith(ORIGIN):
                raise ValueError("Unexpected asset origin")
            name = asset["url"][len(ORIGIN) :]
            match = ASSET.fullmatch(name)
            if not match:
                raise ValueError("Invalid asset path")
            file = stage / "assets" / name
            raw = file.read_bytes()
            digest = hashlib.sha256(raw).digest()
            if (
                digest.hex() != match[1]
                or base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                != asset["hash"]
            ):
                raise ValueError("Asset integrity check failed")
            allowed.add(file)
    else:
        if data["type"] != "rollBackToEmbedded":
            raise ValueError("Invalid rollback directive")
        timestamp = data["parameters"]["commitTime"]
    created_at = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if created_at.tzinfo is None or created_at > dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        minutes=5
    ):
        raise ValueError("Invalid publication time")
    if {p for p in stage.rglob("*") if p.is_file()} != allowed:
        raise ValueError("Unexpected files in publication")
    return pointer, record, allowed, created_at


def publish(
    stage: Path, destination: Path, certificate: Path, channel: str | None = None
):
    pointer, record, allowed, created_at = verify(stage, certificate)
    relative = pointer.relative_to(stage)
    if channel:
        if channel not in {"stable", "preview"}:
            raise ValueError("Invalid channel")
        relative = Path("channels", channel, *relative.parts[2:])
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / ".publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = destination / relative
        if target.is_symlink():
            raise ValueError("Refusing symlink target")
        if target.exists():
            old = json.loads(target.read_bytes())
            old_body = json.loads(
                (
                    destination
                    / "releases"
                    / old["release"]
                    / f"{relative.parts[2]}.{old['kind']}.json"
                ).read_bytes()
            )
            old_time = old_body.get("createdAt") or old_body["parameters"]["commitTime"]
            if created_at < dt.datetime.fromisoformat(old_time.replace("Z", "+00:00")):
                raise ValueError(
                    "Older publications cannot replace newer ones; sign a fresh rollback instead"
                )
        for source in sorted(allowed - {pointer}):
            dest = destination / source.relative_to(stage)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                if dest.is_symlink() or dest.read_bytes() != source.read_bytes():
                    raise ValueError("Immutable publication collision")
            else:
                temporary = dest.with_name(dest.name + "." + uuid.uuid4().hex + ".tmp")
                shutil.copyfile(source, temporary)
                temporary.chmod(0o644)
                os.replace(temporary, dest)
        # Keep the previous pointer as an operator audit trail; consumers see all-or-nothing activation.
        if target.exists():
            history = destination / "history" / (uuid.uuid4().hex + ".json")
            history.parent.mkdir(exist_ok=True)
            history.write_text(
                json.dumps(
                    {
                        "target": str(relative),
                        "previous": json.loads(target.read_bytes()),
                    }
                )
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        with temporary.open("w") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, target)
    return str(relative), record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--channel", choices=["stable", "preview"])
    args = parser.parse_args()
    if args.destination:
        print(
            json.dumps(
                publish(args.stage, args.destination, args.certificate, args.channel)
            )
        )
    else:
        verify(args.stage, args.certificate)
        print("Publication signature, asset integrity and file allowlist verified")
