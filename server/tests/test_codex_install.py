import base64
import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "codex_installer", Path(__file__).resolve().parents[2] / "scripts/install-codex.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def bundle(extra=None):
    prefix = f"package/vendor/{installer.TARGET}/"
    manifest = {
        "layoutVersion": 1,
        "version": "0.153.3",
        "target": installer.TARGET,
        "entrypoint": "bin/codex",
        "resourcesDir": "codex-resources",
        "pathDir": "codex-path",
    }
    files = {
        "codex-package.json": json.dumps(manifest).encode(),
        "bin/codex": b"codex executable",
        "bin/codex-code-mode-host": b"code mode host",
        "codex-resources/bwrap": b"sandbox executable",
        "codex-path/rg": b"search executable",
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(prefix + name)
            entry.size = len(data)
            entry.mode = 0o644 if name.endswith(".json") else 0o755
            archive.addfile(entry, io.BytesIO(data))
        if extra:
            entry = tarfile.TarInfo(prefix + extra)
            entry.size = 4
            archive.addfile(entry, io.BytesIO(b"evil"))
    data = output.getvalue()
    return data, "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def test_native_install_retains_manifest_and_runtime_resources(tmp_path):
    data, integrity = bundle()
    entry = installer.install_bundle(data, "0.153.3", integrity, tmp_path / "bin")
    native = entry.resolve()
    assert entry.is_symlink() and native.read_bytes() == b"codex executable"
    assert native.stat().st_mode & 0o111
    assert (native.parent / "codex-code-mode-host").is_file()
    assert (native.parents[1] / "codex-resources/bwrap").is_file()
    assert (native.parents[1] / "codex-package.json").is_file()
    # Installing the same verified release again is safe and preserves the complete bundle.
    assert installer.install_bundle(data, "0.153.3", integrity, tmp_path / "bin").resolve() == native


def test_native_install_rejects_tampering_before_writing(tmp_path):
    data, integrity = bundle()
    with pytest.raises(ValueError, match="integrity"):
        installer.install_bundle(data + b"changed", "0.153.3", integrity, tmp_path / "bin")
    assert not (tmp_path / "bin").exists()


def test_native_install_rejects_archive_traversal(tmp_path):
    data, integrity = bundle("../../escape")
    with pytest.raises(ValueError, match="Unsafe"):
        installer.install_bundle(data, "0.153.3", integrity, tmp_path / "bin")
    assert not (tmp_path / "bin").exists()


def test_native_install_rejects_wrong_release_manifest(tmp_path):
    data, integrity = bundle()
    with pytest.raises(ValueError, match="manifest"):
        installer.install_bundle(data, "0.153.4", integrity, tmp_path / "bin")
    assert not (tmp_path / "bin").exists()
