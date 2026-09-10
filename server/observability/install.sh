#!/bin/sh
# Run as root on the existing AI Radar host, from this directory. Installs only
# separate, loopback-only services; never restarts the API or changes credentials.
set -eu
test "$(id -u)" = 0
test "$(uname -m)" = x86_64
RADAR_METRICS_ROOT=/opt/ai-radar/observability
RADAR_METRICS_PYTHON=/opt/ai-radar/current/.venv/bin/python
export RADAR_METRICS_ROOT
"$RADAR_METRICS_PYTHON" - <<'PY'
import hashlib, os, shutil, socket, subprocess, sys, tarfile, tempfile, urllib.request
from pathlib import Path
root = Path(os.environ['RADAR_METRICS_ROOT'])
version = '3.14.0'
expected = 'f665c6da19eb7ba399c915d30c7d9793c9b417bf8a749b504bc470678631478d'
root.mkdir(parents=True, exist_ok=True, mode=0o755)
for port, unit in [(18476, 'ai-radar-usage-exporter'), (18477, 'ai-radar-prometheus')]:
    active = subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0
    if not active:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
binary = root / ('prometheus-' + version)
if not binary.exists():
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        archive = Path(temporary) / 'release.tar.gz'
        url = f'https://github.com/prometheus/prometheus/releases/download/v{version}/prometheus-{version}.linux-amd64.tar.gz'
        urllib.request.urlretrieve(url, archive)
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected, 'Release checksum mismatch'
        with tarfile.open(archive) as tar:
            for name in ('prometheus', 'promtool', 'LICENSE', 'NOTICE'):
                member = tar.getmember(f'prometheus-{version}.linux-amd64/{name}')
                assert member.isfile()
                with tar.extractfile(member) as stream:
                    (Path(temporary) / name).write_bytes(stream.read())
        binary.mkdir()
        for name in ('prometheus', 'promtool', 'LICENSE', 'NOTICE'):
            shutil.move(Path(temporary) / name, binary / name)
            (binary / name).chmod(0o755 if name in ('prometheus', 'promtool') else 0o644)
venv = root / 'venv'
if not venv.exists():
    subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
subprocess.run([str(venv/'bin/pip'), 'install', '--quiet', '--require-hashes', '-r', 'requirements.lock'], check=True)
for name in ('exporter.py', 'requirements.lock', 'requirements.in', 'prometheus.yml'):
    shutil.copyfile(name, root / name)
    (root/name).chmod(0o644)
subprocess.run([str(binary/'promtool'), 'check', 'config', str(root/'prometheus.yml')], check=True)
if subprocess.run(['id', 'ai-radar-metrics'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
    subprocess.run(['useradd','--system','--home-dir','/var/lib/ai-radar-metrics','--shell','/usr/sbin/nologin','ai-radar-metrics'],check=True)
subprocess.run(['install','-d','-o','ai-radar-metrics','-g','ai-radar-metrics','-m','0700','/var/lib/ai-radar-metrics'],check=True)
common = '''
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
IPAddressAllow=localhost
UMask=0077
TasksMax=64
CPUQuota=25%
'''
units = {
 'ai-radar-usage-exporter': '''[Unit]
Description=AI Radar OpenTelemetry usage exporter
After=ai-radar.service
[Service]
User=ai-radar
Group=ai-radar
ExecStart=/opt/ai-radar/observability/venv/bin/python /opt/ai-radar/observability/exporter.py --database /var/lib/ai-radar/usage/usage.db --port 18476
MemoryMax=128M
ReadOnlyPaths=-/var/lib/ai-radar/usage
InaccessiblePaths=/etc/ai-radar -/var/lib/ai-radar/codex -/var/lib/ai-radar/radar.db
''' + common,
 'ai-radar-prometheus': f'''[Unit]
Description=AI Radar Prometheus model usage history
After=network.target ai-radar-usage-exporter.service
[Service]
User=ai-radar-metrics
Group=ai-radar-metrics
ExecStart={binary}/prometheus --config.file={root}/prometheus.yml --storage.tsdb.path=/var/lib/ai-radar-metrics --storage.tsdb.retention.time=90d --storage.tsdb.retention.size=512MB --web.listen-address=127.0.0.1:18477 --query.max-concurrency=2 --query.max-samples=100000
MemoryMax=384M
ReadWritePaths=/var/lib/ai-radar-metrics
''' + common,
}
for name, content in units.items():
    content += '\n[Install]\nWantedBy=multi-user.target\n'
    path = Path('/etc/systemd/system') / (name + '.service')
    if path.exists():
        assert path.read_text() == content, 'Existing telemetry unit differs; inspect before replacing'
    else:
        path.write_text(content)
        path.chmod(0o644)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','--now',*[name+'.service' for name in units]],check=True)
print('OpenTelemetry and Prometheus installed on loopback ports 18476 / 18477')
PY
