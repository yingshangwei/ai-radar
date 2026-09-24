from pathlib import Path
import json,base64
import argparse
from pathlib import Path
parser=argparse.ArgumentParser(description="Prepare this host's independent Surge deployment in a private directory")
parser.add_argument('--private-dir',required=True,type=Path)
args=parser.parse_args()
p=args.private_dir.expanduser().resolve()
repo=Path(__file__).resolve().parents[2]
if p==repo or repo in p.parents:
 parser.error('Private output must be outside the repository')
p.mkdir(parents=True,exist_ok=True,mode=0o700)
p.chmod(0o700)
payload={name:base64.b64encode((p/name).read_bytes()).decode() for name in ['server.json','profile-server.json','surge.conf','subscription-url.txt','sync-cert.py']}
body='''#!/bin/sh
set -eu
umask 077
python3 - <<'REMOTE'
import pathlib,json,base64,os,subprocess,hashlib,grp,shutil,socket
payload=PAYLOAD
baseline=json.loads(pathlib.Path('/var/lib/private-surge-ops/baseline.json').read_text())
for f,h in baseline['hashes'].items():
 assert hashlib.sha256(pathlib.Path(f).read_bytes()).hexdigest()==h, 'Existing configuration changed: '+f
assert not pathlib.Path('/etc/private-surge').exists(), 'Refusing to overwrite existing Surge deployment'
for port in (25443,25444):
 s=socket.socket();s.bind(('0.0.0.0',port));s.close()
for user in ('private-surge','private-surge-profile'):
 if subprocess.run(['id',user],capture_output=True).returncode:
  subprocess.run(['useradd','--system','--no-create-home','--shell','/usr/sbin/nologin',user],check=True)
for name,user in [('private-surge','private-surge'),('private-surge-profile','private-surge-profile')]:
 d=pathlib.Path('/etc')/name;d.mkdir(mode=0o750);d.chmod(0o750);os.chown(d,0,grp.getgrnam(user).gr_gid)
opt=pathlib.Path('/opt/private-surge');opt.mkdir(mode=0o755);opt.chmod(0o755)
shutil.copyfile('/opt/private-xray/releases/26.9.9/xray',opt/'xray');(opt/'xray').chmod(0o755)
assert hashlib.sha256((opt/'xray').read_bytes()).digest()==hashlib.sha256(pathlib.Path('/opt/private-xray/releases/26.9.9/xray').read_bytes()).digest()
for name,dest,user,mode in [
 ('server.json','/etc/private-surge/config.json','private-surge',0o640),
 ('profile-server.json','/etc/private-surge-profile/server.json','private-surge-profile',0o640),
 ('surge.conf','/var/lib/private-surge-ops/surge.conf','root',0o600),
 ('subscription-url.txt','/var/lib/private-surge-ops/subscription-url.txt','root',0o600),
 ('sync-cert.py','/opt/private-surge/sync-cert.py','root',0o700)]:
 d=pathlib.Path(dest);d.write_bytes(base64.b64decode(payload[name]));d.chmod(mode);os.chown(d,0,grp.getgrnam(user).gr_gid)
subprocess.run(['/opt/private-surge/sync-cert.py','--initial'],check=True)
common="""Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true
TasksMax=128
LimitNOFILE=32768
LogRateLimitIntervalSec=30s
LogRateLimitBurst=20
"""
xray="""[Unit]
Description=Private Surge Trojan TLS proxy
Wants=network-online.target private-surge-profile.service
After=network-online.target private-surge-profile.service
StartLimitIntervalSec=0
[Service]
Type=simple
User=private-surge
Group=private-surge
ExecStartPre=/opt/private-surge/xray run -test -config /etc/private-surge/config.json
ExecStart=/opt/private-surge/xray run -config /etc/private-surge/config.json
MemoryHigh=128M
MemoryMax=192M
Environment=GOMEMLIMIT=112MiB
CPUQuota=75%
"""+common+"""[Install]
WantedBy=multi-user.target
"""
profile="""[Unit]
Description=Private Surge profile HTTP fallback (loopback only)
After=network.target
StartLimitIntervalSec=0
[Service]
Type=simple
User=private-surge-profile
Group=private-surge-profile
StateDirectory=private-surge-profile
Environment=XDG_DATA_HOME=/var/lib/private-surge-profile/data
Environment=XDG_CONFIG_HOME=/var/lib/private-surge-profile/config
ExecStart=/usr/bin/caddy run --config /etc/private-surge-profile/server.json
MemoryHigh=64M
MemoryMax=96M
Environment=GOMEMLIMIT=48MiB
CPUQuota=25%
"""+common+"""[Install]
WantedBy=multi-user.target
"""
sync="""[Unit]
Description=Validate and synchronize renewed TLS certificate for Surge only
After=caddy.service
[Service]
Type=oneshot
ExecStart=/opt/private-surge/sync-cert.py
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
"""
timer="""[Unit]
Description=Refresh Surge TLS certificate after renewal
[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
RandomizedDelaySec=60
Persistent=true
[Install]
WantedBy=timers.target
"""
for name,content in [('private-surge.service',xray),('private-surge-profile.service',profile),('private-surge-cert-sync.service',sync),('private-surge-cert-sync.timer',timer)]:
 dest=pathlib.Path('/etc/systemd/system')/name;assert not dest.exists();dest.write_text(content);dest.chmod(0o644)
for cmd in [
 ['/opt/private-surge/xray','run','-test','-config','/etc/private-surge/config.json'],
 ['runuser','-u','private-surge-profile','--','/usr/bin/caddy','validate','--config','/etc/private-surge-profile/server.json']]:
 result=subprocess.run(cmd,capture_output=True,text=True)
 if result.returncode: raise SystemExit('Config validation failed; services not activated: '+(result.stdout+result.stderr)[-500:])
print('Both isolated configurations validated')
subprocess.run(['systemctl','daemon-reload'],check=True)
try:
 subprocess.run(['systemctl','enable','--now','private-surge-profile','private-surge'],check=True)
 subprocess.run(['/opt/private-surge/sync-cert.py'],check=True)
 subprocess.run(['systemctl','enable','--now','private-surge-cert-sync.timer'],check=True)
except subprocess.CalledProcessError:
 subprocess.run(['systemctl','disable','--now','private-surge','private-surge-profile','private-surge-cert-sync.timer'],check=False)
 raise
for unit,expected in baseline['services'].items():
 actual=dict(x.split('=',1) for x in subprocess.check_output(['systemctl','show',unit,'-p','MainPID','-p','ActiveState','-p','NRestarts'],text=True).splitlines())
 assert actual==expected,(unit,actual)
for f,h in baseline['hashes'].items():
 assert hashlib.sha256(pathlib.Path(f).read_bytes()).hexdigest()==h,f
print('All 10 original services and 5 protected files unchanged')
for unit in ['private-surge','private-surge-profile','private-surge-cert-sync.timer']:
 print(unit,subprocess.check_output(['systemctl','is-active',unit],text=True).strip())
REMOTE
'''.replace('PAYLOAD',repr(payload))
(p/'install.sh').write_text(body);(p/'install.sh').chmod(0o600)
print('Install bundle created:',len(body),'bytes; private payload not printed')
