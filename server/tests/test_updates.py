import base64
import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from radar.updates import create_app

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("publisher", ROOT / "scripts/publish-updates.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
HEADERS = {
    "expo-protocol-version": "1",
    "expo-platform": "android",
    "expo-runtime-version": "0.13.0",
    "accept": "multipart/mixed",
}


@pytest.fixture
def publication(tmp_path):
    key, cert = tmp_path / "key.pem", tmp_path / "certificate.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=OTA-test",
        ],
        check=True,
        capture_output=True,
    )
    stage = tmp_path / "stage"
    release = str(uuid.uuid4())
    body_path = stage / "releases" / release / "android.manifest.json"
    body_path.parent.mkdir(parents=True)
    raw = b"valid bundle"
    digest = hashlib.sha256(raw).digest()
    filename = digest.hex() + ".bundle"
    asset = stage / "assets" / filename
    asset.parent.mkdir()
    asset.write_bytes(raw)
    data = {
        "id": str(uuid.uuid4()),
        "runtimeVersion": "0.13.0",
        "createdAt": dt.datetime.now(dt.UTC).isoformat(),
        "metadata": {"platform": "android"},
        "assets": [],
        "launchAsset": {
            "url": publisher.ORIGIN + filename,
            "hash": base64.urlsafe_b64encode(digest).rstrip(b"=").decode(),
        },
    }
    pointer = stage / "channels/stable/android/0.13.0.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"release": release, "kind": "manifest"}))

    def resign(payload=data, kind="manifest"):
        body = body_path.with_name(f"android.{kind}.json")
        body.write_text(json.dumps(payload))
        signature = subprocess.check_output(["openssl", "dgst", "-sha256", "-sign", str(key), str(body)])
        body.with_suffix(".sig").write_bytes(base64.b64encode(signature))
        return body

    resign()
    return stage, cert, body_path, asset, data, resign


def test_signed_publication_protocol_and_atomic_activation(publication, tmp_path):
    stage, cert, _, asset, data, _ = publication
    target = tmp_path / "live"
    publisher.publish(stage, target, cert)
    client = TestClient(create_app(target))
    response = client.get("/updates/v1/manifest", headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["expo-protocol-version"] == "1"
    assert 'name="manifest"' in response.text and "expo-signature:" in response.text
    assert data["id"] in response.text
    assert (
        client.get(
            "/updates/v1/manifest", headers={**HEADERS, "expo-current-update-id": data["id"]}
        ).status_code
        == 204
    )
    for change in [
        {"expo-platform": "ios"},
        {"expo-runtime-version": "0.12.0"},
        {"expo-channel-name": "preview"},
    ]:
        assert client.get("/updates/v1/manifest", headers={**HEADERS, **change}).status_code == 204
    download = client.get("/updates/assets/" + asset.name)
    assert download.content == asset.read_bytes() and "immutable" in download.headers["cache-control"]
    json_response = client.get("/updates/v1/manifest", headers={**HEADERS, "accept": "application/expo+json"})
    assert json_response.json() == data and "expo-signature" in json_response.headers


@pytest.mark.parametrize("fault", ["body", "signature", "asset", "missing", "extra", "symlink"])
def test_bad_publication_never_changes_live_pointer(publication, tmp_path, fault):
    stage, cert, body, asset, _, _ = publication
    target = tmp_path / "live"
    publisher.publish(stage, target, cert)
    before = (target / "channels/stable/android/0.13.0.json").read_bytes()
    if fault == "body":
        body.write_bytes(body.read_bytes() + b" ")
    elif fault == "signature":
        body.with_suffix(".sig").write_text(base64.b64encode(b"x" * 256).decode())
    elif fault == "asset":
        asset.write_bytes(b"tampered")
    elif fault == "missing":
        asset.unlink()
    elif fault == "extra":
        (stage / "private-key.pem").write_text("not publishable")
    else:
        (stage / "link").symlink_to(cert)
    with pytest.raises((ValueError, OSError, subprocess.CalledProcessError)):
        publisher.publish(stage, target, cert)
    assert (target / "channels/stable/android/0.13.0.json").read_bytes() == before


def test_rollback_signed_directive(publication, tmp_path):
    stage, cert, body, asset, data, resign = publication
    rollback = {
        "type": "rollBackToEmbedded",
        "parameters": {"commitTime": dt.datetime.now(dt.UTC).isoformat()},
    }
    resign(rollback, "rollback")
    pointer = stage / "channels/stable/android/0.13.0.json"
    record = json.loads(pointer.read_bytes())
    record["kind"] = "rollback"
    pointer.write_text(json.dumps(record))
    body.unlink()
    body.with_suffix(".sig").unlink()
    asset.unlink()
    target = tmp_path / "live"
    publisher.publish(stage, target, cert)
    client = TestClient(create_app(target))
    result = client.get("/updates/v1/manifest", headers=HEADERS)
    assert (
        result.status_code == 200
        and 'name="directive"' in result.text
        and "rollBackToEmbedded" in result.text
    )
    assert (
        client.get("/updates/v1/manifest", headers={**HEADERS, "accept": "application/json"}).status_code
        == 406
    )
    assert (
        client.get(
            "/updates/v1/manifest",
            headers={**HEADERS, "expo-current-update-id": data["id"], "expo-embedded-update-id": data["id"]},
        ).status_code
        == 204
    )


@pytest.mark.parametrize(
    "change,status",
    [
        ({"expo-protocol-version": "0"}, 400),
        ({"expo-platform": "web"}, 400),
        ({"expo-runtime-version": "../secrets"}, 400),
        ({"expo-channel-name": "../stable"}, 400),
        ({"accept": "text/html"}, 406),
        ({"accept": "multipart/mixed;q=0,*/*;q=0"}, 406),
    ],
)
def test_protocol_rejects_invalid_requests(tmp_path, change, status):
    assert (
        TestClient(create_app(tmp_path))
        .get("/updates/v1/manifest", headers={**HEADERS, **change})
        .status_code
        == status
    )


def test_missing_channel_is_offline_safe_and_private_paths_are_inaccessible(tmp_path):
    client = TestClient(create_app(tmp_path))
    response = client.get("/updates/v1/manifest", headers=HEADERS)
    assert response.status_code == 204 and not response.content
    for path in [
        "/updates/assets/private-key.pem",
        "/updates/releases/secret",
        "/updates/channels/stable/android/0.13.0.json",
        "/openapi.json",
    ]:
        assert client.get(path).status_code == 404
