"""Exercise release activation on a temporary filesystem with fake OS service commands.

No root privileges, network, real services or production paths are used. Dependency
installation and systemd are simulated; this is not a claim of a Linux deployment.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def deployment(tmp_path):
    base = tmp_path / "opt/ai-radar"
    release = base / "releases/new"
    old = base / "releases/old"
    config = tmp_path / "etc/ai-radar"
    unit = tmp_path / "etc/systemd/system/ai-radar.service"
    for path in [
        release / "scripts",
        release / "server",
        release / "deploy",
        old / ".venv",
        config,
        unit.parent,
    ]:
        path.mkdir(parents=True, exist_ok=True)
    (old / ".venv/dependencies").write_text("original dependencies")
    (release / "server/requirements.lock").write_text("locked dependencies")
    (release / "server/config.toml").write_text("new defaults")
    (release / "deploy/ai-radar.service").write_text("new service")
    (config / "config.toml").write_text("existing user configuration")
    (config / "server.env").write_text("existing user authorization")
    unit.write_text("old service")
    (base / "current").symlink_to(old)
    script = (ROOT / "scripts/install-server.sh").read_text()
    for production in ["/opt/ai-radar", "/etc/ai-radar", "/var/lib/ai-radar", "/etc/systemd/system"]:
        script = script.replace(production, str(tmp_path / production.lstrip("/")))
    installer = release / "scripts/install-server.sh"
    installer.write_text(script)
    state = tmp_path / "service.json"
    state.write_text(json.dumps({"active": True, "enabled": True, "calls": []}))
    shims = tmp_path / "bin"
    shims.mkdir()

    def shim(name, body):
        target = shims / name
        target.write_text(f"#!{sys.executable}\n" + body)
        target.chmod(0o755)

    shim("id", "import sys\nif '-u' in sys.argv: print('0')\n")
    shim("ss", "")
    shim("sleep", "")
    shim("curl", "import os,sys\nsys.exit(1 if os.environ.get('FAIL_HEALTH') else 0)\n")
    shim(
        "install",
        """import sys, pathlib, shutil
args = sys.argv[1:]
if '-d' in args:
    pathlib.Path(args[-1]).mkdir(parents=True, exist_ok=True)
else:
    shutil.copyfile(args[-2], args[-1])
""",
    )
    shim(
        "python3",
        """import os, pathlib, subprocess, sys
if sys.argv[1:3] == ['-m', 'venv']:
    target = pathlib.Path(sys.argv[3]) / 'bin/pip'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('#!/bin/sh\\nprintf new > "$(dirname "$0")/../dependencies"\\n[ -z "$FAIL_DEPS" ]\\n')
    target.chmod(0o755)
else:
    sys.exit(subprocess.call([sys.executable, *sys.argv[1:]]))
""",
    )
    shim(
        "systemctl",
        """import json, os, pathlib, sys
path = pathlib.Path(os.environ['SERVICE_STATE'])
state = json.loads(path.read_text())
command = sys.argv[1]
state['calls'].append(sys.argv[1:])
result = 0
if command == 'is-active': result = int(not state['active'])
elif command == 'is-enabled': result = int(not state['enabled'])
elif command in ['enable', 'disable']: state['enabled'] = command == 'enable'
elif command == 'stop': state['active'] = False
elif command == 'restart':
    current = pathlib.Path(os.environ['CURRENT_LINK']).resolve()
    result = int(bool(os.environ.get('FAIL_START')) and current.name == 'new')
    state['active'] = result == 0
path.write_text(json.dumps(state))
sys.exit(result)
""",
    )
    env = {
        **os.environ,
        "PATH": f"{shims}:/usr/bin:/bin",
        "SERVICE_STATE": str(state),
        "CURRENT_LINK": str(base / "current"),
    }

    def run(**flags):
        return subprocess.run(
            ["/bin/bash", str(installer)], env={**env, **flags}, capture_output=True, text=True
        )

    return {
        "run": run,
        "base": base,
        "release": release,
        "old": old,
        "state": state,
        "unit": unit,
        "config": config,
    }


def test_successful_upgrade_preserves_old_dependencies_and_user_config(deployment):
    d = deployment
    result = d["run"]()
    assert result.returncode == 0, result.stderr
    assert (d["base"] / "current").resolve() == d["release"]
    assert (d["old"] / ".venv/dependencies").read_text() == "original dependencies"
    assert (d["release"] / ".venv/dependencies").read_text() == "new"
    assert (d["config"] / "server.env").read_text() == "existing user authorization"
    assert (d["config"] / "config.toml").read_text() == "existing user configuration"


@pytest.mark.parametrize("flag", ["FAIL_START", "FAIL_HEALTH", "FAIL_DEPS"])
def test_failed_upgrade_keeps_old_release_runnable(deployment, flag):
    d = deployment
    result = d["run"](**{flag: "1"})
    assert result.returncode != 0
    assert (d["base"] / "current").resolve() == d["old"]
    assert (d["old"] / ".venv/dependencies").read_text() == "original dependencies"
    assert d["unit"].read_text() == "old service"
    state = json.loads(d["state"].read_text())
    assert state["active"] and state["enabled"]
    assert all(call[-1] in ("ai-radar", "daemon-reload") for call in state["calls"])


def test_failed_first_install_leaves_new_service_stopped(deployment):
    d = deployment
    (d["base"] / "current").unlink()
    d["unit"].unlink()
    d["state"].write_text(json.dumps({"active": False, "enabled": False, "calls": []}))
    result = d["run"](FAIL_HEALTH="1")
    assert result.returncode != 0
    state = json.loads(d["state"].read_text())
    assert not state["active"] and not state["enabled"]


def test_active_release_cannot_be_overwritten(deployment):
    d = deployment
    (d["base"] / "current").unlink()
    (d["base"] / "current").symlink_to(d["release"])
    result = d["run"]()
    assert result.returncode != 0 and "already active" in result.stderr
    assert not (d["release"] / ".venv").exists()
    assert json.loads(d["state"].read_text())["calls"] == []
