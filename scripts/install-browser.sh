#!/usr/bin/env bash
# Prepare the optional browser service; run before activating the reviewed release.
# Does not restart the API, Caddy, or any unrelated service.
set -euo pipefail
umask 022
RADAR_RELEASE="$(cd "$(dirname "$0")/.." && pwd)"
[[ "$(id -u)" == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
if [[ -n "$(ss -H -ltn 'sport = :18475')" ]] && ! systemctl is-active --quiet ai-radar-browser; then
    echo 'Browser port is occupied by another service.' >&2; exit 1
fi
export NEEDRESTART_MODE=l
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends xvfb x11vnc openbox fonts-noto-cjk
python3 -m venv "$RADAR_RELEASE/.venv"
"$RADAR_RELEASE/.venv/bin/pip" install --require-hashes -r "$RADAR_RELEASE/server/requirements.lock"
"$RADAR_RELEASE/.venv/bin/pip" install --no-deps -e "$RADAR_RELEASE/server"
"$RADAR_RELEASE/.venv/bin/playwright" install-deps chromium
PLAYWRIGHT_BROWSERS_PATH=/opt/ai-radar/browser-cache "$RADAR_RELEASE/.venv/bin/playwright" install --no-shell chromium
id ai-radar-browser >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/ai-radar-browser --shell /usr/sbin/nologin ai-radar-browser
install -d -m 0700 -o ai-radar-browser -g ai-radar-browser /var/lib/ai-radar-browser
python3 - <<'PY'
import base64,hashlib,io,json,os,secrets,tarfile,urllib.request
from pathlib import Path
url='https://registry.npmjs.org/@novnc/novnc/-/novnc-1.7.0.tgz'
integrity='ucEJOx4T2avIRCleodk7YobZj5O2Ga2AeLfQ69A/yjG9HHba2+PDgwSkN3FttrmG+70ZGx21sElNFouK13RzyA=='
data=urllib.request.urlopen(url,timeout=30).read(2_000_000)
assert base64.b64encode(hashlib.sha512(data).digest()).decode()==integrity
root=Path('/opt/ai-radar/browser-assets-1.7.0');root.mkdir(exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(data)) as archive:
 for member in archive.getmembers():
  assert not member.name.startswith('/') and '..' not in Path(member.name).parts
  if member.isfile() and member.name.startswith('package/'):
   target=root/member.name.removeprefix('package/')
   target.parent.mkdir(parents=True,exist_ok=True)
   target.write_bytes(archive.extractfile(member).read())
link=Path('/opt/ai-radar/browser-assets')
if not link.exists():link.symlink_to(root)
elif link.resolve()!=root:raise RuntimeError('Existing noVNC assets differ; review upgrade first')
env=Path('/etc/ai-radar/browser.env')
if not env.exists():
 fd=os.open(env,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as f:
  f.write('RADAR_BROWSER_WORKER_TOKEN='+secrets.token_urlsafe(48)+'\n')
  f.write('RADAR_BROWSER_PUBLIC_ORIGIN=https://radar.yswdra.cn\n')
print('Browser dependencies, private credentials and integrity-checked noVNC assets prepared.')
PY
"$RADAR_RELEASE/.venv/bin/python" "$RADAR_RELEASE/scripts/patch-novnc.py" /opt/ai-radar/browser-assets
# Ubuntu 24.04 restricts unprivileged user namespaces by executable. Permit
# Chromium's own sandbox only for this dedicated installation, never globally.
if command -v apparmor_parser >/dev/null; then
    cat > /etc/apparmor.d/ai-radar-browser <<'APPARMOR'
abi <abi/4.0>,
include <tunables/global>
profile ai-radar-browser /opt/ai-radar/browser-cache/**/chrome flags=(unconfined) {
  userns,
}
APPARMOR
    apparmor_parser -r /etc/apparmor.d/ai-radar-browser
fi
echo 'Preparation complete. Activate the API and browser service together after verification.'
