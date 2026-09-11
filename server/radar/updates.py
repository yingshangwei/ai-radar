"""Read-only Expo Updates v1 distribution, isolated from the content API.

Protocol: https://docs.expo.dev/technical-specs/expo-updates-1/
SDK and protocol behavior were checked against expo/custom-expo-updates-server.
Signing and publication happen offline; this process has no private keys or write API.
"""

import json
import os
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

RUNTIME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
ASSET = re.compile(r"[0-9a-f]{64}\.(?:bundle|png|jpg|jpeg|webp|gif|ttf|otf|woff|woff2|bin)\Z")
MIMES = {
    "bundle": "application/javascript",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "ttf": "font/ttf",
    "otf": "font/otf",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "bin": "application/octet-stream",
}
HEADERS = {
    "expo-protocol-version": "1",
    "expo-sfv-version": "0",
    "expo-manifest-filters": "",
    "expo-server-defined-headers": "",
    "cache-control": "private, no-store",
    "vary": "expo-platform, expo-runtime-version, expo-channel-name, accept",
}


def small_file(root: Path, path: Path, limit: int):
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Invalid publication path")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Publication exceeds limit")
    return data


def accepts(header: str, kind: str):
    """Most specific media range wins, including explicit q=0 exclusions."""
    selected = (-1, 0.0)
    for entry in header[:4096].split(","):
        media, *params = entry.strip().lower().split(";")
        specificity = (
            2 if media == kind else 1 if media == kind.split("/")[0] + "/*" else 0 if media == "*/*" else -1
        )
        if specificity < 0:
            continue
        quality = 1.0
        for param in params:
            if param.strip().startswith("q="):
                try:
                    quality = float(param.strip()[2:])
                except ValueError:
                    quality = 0.0
        if not 0 <= quality <= 1:
            quality = 0.0
        if specificity > selected[0]:
            selected = (specificity, quality)
    return selected[1]


def create_app(root: Path):
    service = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @service.get("/updates/healthz")
    def health():
        return {"ok": root.is_dir(), "service": "expo-updates", "protocol": 1}

    @service.get("/updates/v1/manifest")
    def manifest(request: Request):
        headers = request.headers
        platform = headers.get("expo-platform")
        runtime = headers.get("expo-runtime-version", "")
        channel = headers.get("expo-channel-name", "stable")
        if headers.get("expo-protocol-version") != "1" or platform not in {"android", "ios"}:
            raise HTTPException(400, "Unsupported Expo protocol or platform")
        if not RUNTIME.fullmatch(runtime) or channel not in {"stable", "preview"}:
            raise HTTPException(400, "Invalid runtime or channel")
        accept = headers.get("accept", "")
        formats = ["multipart/mixed", "application/expo+json", "application/json"]
        selected = max(formats, key=lambda media: accepts(accept, media))
        if not accepts(accept, selected):
            raise HTTPException(406, "Unsupported response format")

        def no_update():
            if not accepts(accept, "multipart/mixed"):
                raise HTTPException(406, "No update; multipart support required")
            return Response(status_code=204, headers=HEADERS)

        pointer = root / "channels" / channel / platform / f"{runtime}.json"
        if not pointer.exists():
            return no_update()
        try:
            record = json.loads(small_file(root, pointer, 4096))
            release = str(uuid.UUID(record["release"]))
            kind = record["kind"]
            if kind not in {"manifest", "rollback"}:
                raise ValueError("Invalid publication kind")
            folder = root / "releases" / release
            body = small_file(root, folder / f"{platform}.{kind}.json", 256_000)
            data = json.loads(body)
            signature = small_file(root, folder / f"{platform}.{kind}.sig", 2048).decode("ascii").strip()
            if not re.fullmatch(r"[A-Za-z0-9+/]{100,1800}={0,2}", signature):
                raise ValueError("Invalid signature")
            if kind == "manifest":
                if data["runtimeVersion"] != runtime or data["metadata"]["platform"] != platform:
                    raise ValueError("Incompatible publication")
                if data["id"] == headers.get("expo-current-update-id"):
                    return no_update()
            else:
                if data["type"] != "rollBackToEmbedded" or not data["parameters"]["commitTime"]:
                    raise ValueError("Invalid rollback")
                if not accepts(accept, "multipart/mixed"):
                    raise HTTPException(406, "Rollback requires multipart support")
                selected = "multipart/mixed"
                embedded = headers.get("expo-embedded-update-id")
                if embedded and embedded == headers.get("expo-current-update-id"):
                    return no_update()
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(503, "Update publication unavailable") from None
        sig_header = f'sig="{signature}", keyid="ai-radar-ota-2026", alg="rsa-v1_5-sha256"'
        if selected != "multipart/mixed":
            return Response(body, media_type=selected, headers={**HEADERS, "expo-signature": sig_header})
        boundary = "radar-" + uuid.uuid4().hex
        part = "manifest" if kind == "manifest" else "directive"
        prefix = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{part}"\r\n'
            f"Content-Type: application/json\r\nexpo-signature: {sig_header}\r\n\r\n"
        ).encode()
        return Response(
            prefix + body + f"\r\n--{boundary}--\r\n".encode(),
            media_type=f"multipart/mixed; boundary={boundary}",
            headers=HEADERS,
        )

    @service.get("/updates/assets/{name}")
    def asset(name: str):
        if not ASSET.fullmatch(name):
            raise HTTPException(404)
        path = root / "assets" / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(
            path,
            media_type=MIMES[name.rsplit(".", 1)[1]],
            headers={
                "cache-control": "public, max-age=31536000, immutable",
                "x-content-type-options": "nosniff",
            },
        )

    return service


app = create_app(Path(os.environ.get("RADAR_UPDATES_ROOT", "/var/lib/ai-radar-updates")))
