#!/usr/bin/env python3
"""Install a pinned official Codex native bundle, verifying npm SHA-512 integrity."""

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

TARGET = "x86_64-unknown-linux-musl"


def install_bundle(
    data: bytes, version: str, integrity: str, destination: Path
) -> Path:
    actual = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if actual != integrity:
        raise ValueError("Package integrity mismatch")
    prefix = PurePosixPath("package/vendor") / TARGET
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        manifest_file = archive.extractfile(str(prefix / "codex-package.json"))
        if manifest_file is None:
            raise ValueError("Missing official native package manifest")
        manifest = json.load(manifest_file)
        if any(
            manifest.get(key) != value
            for key, value in {
                "layoutVersion": 1,
                "version": version,
                "target": TARGET,
                "entrypoint": "bin/codex",
                "resourcesDir": "codex-resources",
                "pathDir": "codex-path",
            }.items()
        ):
            raise ValueError("Unexpected native package manifest")
        members = []
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if not path.is_relative_to(prefix):
                continue
            relative = path.relative_to(prefix)
            if ".." in relative.parts or not member.isfile():
                raise ValueError("Unsafe native package entry")
            members.append((member, relative))
        required = {
            "bin/codex",
            "bin/codex-code-mode-host",
            "codex-resources/bwrap",
            "codex-path/rg",
        }
        if not required.issubset({str(relative) for _, relative in members}):
            raise ValueError("Incomplete native bundle")
        destination.mkdir(parents=True, exist_ok=True)
        packages = destination.parent / "codex-packages"
        packages.mkdir(mode=0o755, exist_ok=True)
        bundle = packages / f"{version}-{TARGET}"
        if bundle.exists():
            if (bundle / ".archive-integrity").read_text() != integrity:
                raise ValueError("An existing bundle has a different integrity value")
        else:
            staged = Path(tempfile.mkdtemp(prefix=".install-", dir=packages))
            try:
                staged.chmod(0o755)
                for member, relative in members:
                    target = staged / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        archive.extractfile(member) as source,
                        target.open("xb") as output,
                    ):
                        shutil.copyfileobj(source, output)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
                (staged / ".archive-integrity").write_text(integrity)
                os.rename(staged, bundle)
            except BaseException:
                shutil.rmtree(staged)
                raise
    link = destination / "codex.new"
    link.symlink_to(bundle / "bin/codex")
    os.replace(link, destination / "codex")
    return destination / "codex"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.153.3")
    parser.add_argument(
        "--destination", type=Path, default=Path("/opt/ai-radar/tools/bin")
    )
    args = parser.parse_args()
    if not args.version or any(c not in "0123456789." for c in args.version):
        parser.error("Expected a pinned numeric release version")
    with urllib.request.urlopen(
        f"https://registry.npmjs.org/@openai/codex/{args.version}-linux-x64",
        timeout=30,
    ) as response:
        metadata = json.load(response)
    url = metadata["dist"]["tarball"]
    if not url.startswith("https://registry.npmjs.org/@openai/codex/"):
        raise ValueError("Unexpected package origin")
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read(250_000_000)
    target = install_bundle(
        data, args.version, metadata["dist"]["integrity"], args.destination
    )
    print("Installed verified official Codex native bundle", args.version, "at", target)


if __name__ == "__main__":
    main()
