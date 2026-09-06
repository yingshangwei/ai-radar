#!/usr/bin/env python3
"""Run the official Gradle wrapper using the same local credentials as EAS."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=["Debug", "Release"], default="Release")
    parser.add_argument(
        "--credentials", type=Path, default=ROOT / "app/credentials.json"
    )
    parser.add_argument("--google-alternate-host", action="store_true")
    parser.add_argument("--proxy", help="Optional HTTP proxy URL without credentials")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        parser.error("--workers must be between 1 and 16")
    env = os.environ.copy()
    if args.variant == "Release":
        if not args.credentials.is_file():
            parser.error("Release credentials are missing; see docs/ANDROID.md")
        if args.credentials.stat().st_mode & 0o077:
            parser.error("Private credentials must use file permissions 0600")
        credentials = json.loads(args.credentials.read_text())["android"]["keystore"]
        key = Path(credentials["keystorePath"])
        if not key.is_absolute():
            key = args.credentials.resolve().parent / key
        if not key.is_file():
            parser.error("The configured keystore does not exist")
        env.update(
            {
                "AIRADAR_ANDROID_KEYSTORE": str(key.resolve()),
                "AIRADAR_ANDROID_STORE_PASSWORD": credentials["keystorePassword"],
                "AIRADAR_ANDROID_KEY_ALIAS": credentials["keyAlias"],
                "AIRADAR_ANDROID_KEY_PASSWORD": credentials.get(
                    "keyPassword", credentials["keystorePassword"]
                ),
                "NODE_ENV": "production",
            }
        )
    command = [
        "./gradlew",
        f":app:assemble{args.variant}",
        "--no-daemon",
        f"--max-workers={args.workers}",
    ]
    if args.google_alternate_host:
        command += ["--init-script", str(ROOT / "scripts/google-maven.init.gradle")]
    if args.proxy:
        proxy = urlparse(args.proxy)
        if (
            proxy.scheme != "http"
            or not proxy.hostname
            or proxy.username
            or proxy.password
            or proxy.path not in ("", "/")
            or proxy.query
            or proxy.fragment
        ):
            parser.error(
                "--proxy must be an HTTP proxy URL without credentials, query or path"
            )
        for protocol in ["http", "https"]:
            command += [
                f"-D{protocol}.proxyHost={proxy.hostname}",
                f"-D{protocol}.proxyPort={proxy.port or 80}",
            ]
    command += [
        "-Dorg.gradle.internal.http.connectionTimeout=15000",
        "-Dorg.gradle.internal.http.socketTimeout=30000",
    ]
    result = subprocess.run(command, cwd=ROOT / "app/android", env=env, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
    for apk in sorted(
        (ROOT / f"app/android/app/build/outputs/apk/{args.variant.lower()}").glob(
            "*.apk"
        )
    ):
        with apk.open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        print(f"APK: {apk}\nSHA256: {digest.hexdigest()}")


if __name__ == "__main__":
    main()
