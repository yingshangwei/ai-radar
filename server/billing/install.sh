#!/bin/sh
# Install only the isolated, hash-pinned official Alibaba billing SDK.
set -eu
RADAR_BILLING_ROOT="${RADAR_BILLING_ROOT:-/opt/ai-radar/billing}"
RADAR_BILLING_BOOTSTRAP="${RADAR_BILLING_BOOTSTRAP:-/opt/ai-radar/current/.venv/bin/python}"
export RADAR_BILLING_ROOT
"$RADAR_BILLING_BOOTSTRAP" - <<'PY'
import os
import subprocess
import sys
from pathlib import Path

root = Path(os.environ['RADAR_BILLING_ROOT'])
root.mkdir(parents=True, exist_ok=True)
assert not root.is_symlink()
venv = root / 'venv'
if not venv.exists():
    subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
subprocess.run([str(venv/'bin/python'), '-m', 'pip', 'install', '--quiet', '--require-hashes',
    '-r', str(Path(__file__).parent/'requirements.lock') if '__file__' in globals() and __file__ != '<stdin>'
    else 'requirements.lock'], check=True)
subprocess.run([str(venv/'bin/python'), '-m', 'pip', 'check'], check=True)
print('Isolated Alibaba billing SDK ready; no inference services changed.')
PY
