#!/usr/bin/env bash
# First install only. Package contains updates.py, publish-updates.py, certificate.pem, service unit.
set -euo pipefail
umask 022
[[ "$(id -u)" == 0 && $# == 2 ]] || { echo 'Usage as root: install-updates.sh PACKAGE_DIR EXPECTED_CADDY_SHA256'; exit 1; }
RADAR_PACKAGE="$(cd "$1" && pwd)"
RADAR_EXPECTED_CADDY="$2"
[[ ! -e /opt/ai-radar/updates ]] || { echo 'Update service already exists; review a service upgrade separately.'; exit 1; }
[[ -z "$(ss -H -ltn 'sport = :18478')" ]] || { echo 'Port 18478 already in use.'; exit 1; }
[[ "$(sha256sum /etc/caddy/Caddyfile | cut -d ' ' -f 1)" == "$RADAR_EXPECTED_CADDY" ]] || { echo 'Caddy changed since preflight; inspect before installing.'; exit 1; }
RADAR_API_PID="$(systemctl show ai-radar -p MainPID --value)"
install -d -m 0755 /opt/ai-radar/updates /var/lib/ai-radar-updates
cp -p /etc/caddy/Caddyfile /opt/ai-radar/updates/Caddyfile.before-updates
rollback() {
  local result=$?
  trap - EXIT
  if [[ "$result" == 0 ]]; then return; fi
  set +e
  cp -p /opt/ai-radar/updates/Caddyfile.before-updates /etc/caddy/Caddyfile
  systemctl reload caddy
  systemctl disable --now ai-radar-updates
  echo 'Installation failed; restored previous Caddy configuration and stopped update service.' >&2
  exit "$result"
}
trap rollback EXIT
id ai-radar-updates >/dev/null 2>&1 || useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin ai-radar-updates
for file in updates.py publish-updates.py certificate.pem; do
  install -m 0644 "$RADAR_PACKAGE/$file" "/opt/ai-radar/updates/$file"
done
# Share the already locked Python runtime without changing any installed dependencies.
ln -s /opt/ai-radar/current/.venv /opt/ai-radar/updates/venv
install -m 0644 "$RADAR_PACKAGE/ai-radar-updates.service" /etc/systemd/system/ai-radar-updates.service
systemctl daemon-reload
systemctl enable --now ai-radar-updates
RADAR_HEALTHY=false
for attempt in 1 2 3 4 5; do
  if curl -fsS --max-time 3 http://127.0.0.1:18478/updates/healthz; then RADAR_HEALTHY=true; break; fi
  sleep 1
done
[[ "$RADAR_HEALTHY" == true ]]
python3 - <<'PY'
from pathlib import Path
p = Path('/etc/caddy/Caddyfile')
text = p.read_text()
needle = 'reverse_proxy 127.0.0.1:18473'
if text.count(needle) != 1:
    raise SystemExit('Expected exactly one existing Radar API route')
text = text.replace(needle, 'handle /updates/* {\n        reverse_proxy 127.0.0.1:18478\n    }\n    handle {\n        reverse_proxy 127.0.0.1:18473\n    }')
Path('/opt/ai-radar/updates/Caddyfile.candidate').write_text(text)
PY
caddy validate --config /opt/ai-radar/updates/Caddyfile.candidate --adapter caddyfile
install -m 0644 /opt/ai-radar/updates/Caddyfile.candidate /etc/caddy/Caddyfile
systemctl reload caddy
curl -fsS --max-time 10 --resolve radar.yswdra.cn:443:127.0.0.1 https://radar.yswdra.cn/updates/healthz
curl -fsS --max-time 10 http://127.0.0.1:18473/healthz
[[ "$(systemctl show ai-radar -p MainPID --value)" == "$RADAR_API_PID" ]]
trap - EXIT
echo 'Update distribution installed; existing API PID unchanged.'
