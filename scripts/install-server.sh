#!/usr/bin/env bash
# Install an already-uploaded, reviewed release on Ubuntu; does not change firewall, DNS or reverse proxy.
set -euo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root on the target server.' >&2; exit 1; }
RADAR_RELEASE="$(cd "$(dirname "$0")/.." && pwd)"
RADAR_PREVIOUS="$(readlink -f /opt/ai-radar/current 2>/dev/null || true)"
if [[ -n "$(ss -H -ltn 'sport = :18473')" ]] && ! systemctl is-active --quiet ai-radar; then
    echo 'Port 18473 belongs to another service; stopping.' >&2; exit 1
fi
if ! python3 -m venv /opt/ai-radar/venv; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
    python3 -m venv /opt/ai-radar/venv
fi
/opt/ai-radar/venv/bin/pip install --require-hashes -r "$RADAR_RELEASE/server/requirements.lock"
/opt/ai-radar/venv/bin/pip install --no-deps -e "$RADAR_RELEASE/server"
id ai-radar >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/ai-radar --shell /usr/sbin/nologin ai-radar
install -d -m 0750 -o ai-radar -g ai-radar /var/lib/ai-radar
install -d -m 0700 -o ai-radar -g ai-radar /var/lib/ai-radar/codex
install -d -m 0755 /etc/ai-radar
if [[ ! -f /etc/ai-radar/config.toml ]]; then
    install -m 0644 "$RADAR_RELEASE/server/config.toml" /etc/ai-radar/config.toml
fi
if [[ ! -f /etc/ai-radar/server.env ]]; then
    python3 - <<'PY'
import os, secrets
fd=os.open('/etc/ai-radar/server.env',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:
    f.write('RADAR_READER_TOKEN='+secrets.token_urlsafe(32)+'\n')
    f.write('RADAR_ADMIN_TOKEN='+secrets.token_urlsafe(32)+'\n')
    f.write('RADAR_SCHEDULER_ENABLED=false\n')
PY
fi
ln -sfn "$RADAR_RELEASE" /opt/ai-radar/current
install -m 0644 "$RADAR_RELEASE/deploy/ai-radar.service" /etc/systemd/system/ai-radar.service
systemctl daemon-reload
systemctl enable ai-radar
systemctl restart ai-radar
RADAR_HEALTHY=false
for attempt in 1 2 3 4 5; do
    if curl --silent --fail --max-time 3 http://127.0.0.1:18473/healthz; then
        RADAR_HEALTHY=true; break
    fi
    sleep 2
done
if [[ "$RADAR_HEALTHY" != true ]]; then
    if [[ -n "$RADAR_PREVIOUS" && "$RADAR_PREVIOUS" != "$RADAR_RELEASE" ]]; then
        ln -sfn "$RADAR_PREVIOUS" /opt/ai-radar/current
        systemctl restart ai-radar
    fi
    echo 'Health check failed. Review ai-radar service logs.' >&2; exit 1
fi
echo 'AI Radar is running on localhost:18473. Automatic jobs remain disabled until source/model authorization.'
echo 'Existing services, public ports and DNS were not modified.'
