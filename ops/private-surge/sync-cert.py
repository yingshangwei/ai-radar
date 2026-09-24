#!/usr/bin/python3
"""Copy only a validated renewed certificate, then restart only private-surge."""
import fcntl,hashlib,json,os,pathlib,ssl,subprocess,sys,grp,tempfile
os.umask(0o027)
root=pathlib.Path('/etc/private-surge/tls')
root.mkdir(mode=0o750,exist_ok=True)
lock=open('/var/lib/private-surge-ops/cert.lock','w');fcntl.flock(lock,fcntl.LOCK_EX)
sources=list(pathlib.Path('/var/lib/caddy/.local/share/caddy/certificates').glob('*/radar.yswdra.cn/radar.yswdra.cn.crt'))
valid=[]
for crt in sources:
 key=crt.with_suffix('.key')
 if not key.is_file(): continue
 if subprocess.run(['openssl','x509','-in',str(crt),'-noout','-checkend','86400'],capture_output=True).returncode: continue
 if subprocess.run(['openssl','x509','-in',str(crt),'-noout','-checkhost','radar.yswdra.cn'],capture_output=True).returncode: continue
 try:
  ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(crt,key)
 except (ssl.SSLError,OSError): continue
 end=ssl.cert_time_to_seconds(ssl._ssl._test_decode_cert(str(crt))['notAfter'])
 valid.append((end,crt.read_bytes(),key.read_bytes()))
if not valid: raise SystemExit('No valid matching source certificate; existing certificate preserved')
_,cert,key=max(valid,key=lambda v:v[0]); digest=hashlib.sha256(cert+key).hexdigest()
gid=grp.getgrnam('private-surge').gr_gid
os.chown(root,0,gid);root.chmod(0o750)
generation=root/digest
if not generation.exists():
 generation.mkdir(mode=0o750);generation.chmod(0o750);os.chown(generation,0,gid)
 for name,data in [('fullchain.pem',cert),('key.pem',key)]:
  p=generation/name;p.write_bytes(data);p.chmod(0o640);os.chown(p,0,gid)
current=root/'current'; marker=pathlib.Path('/var/lib/private-surge-ops/active-cert.sha256')
old=os.readlink(current) if current.is_symlink() else None
if old==digest and marker.exists() and marker.read_text()==digest:
 print('Certificate unchanged');sys.exit(0)
def link(target):
 tmp=root/('current-'+str(os.getpid()));tmp.symlink_to(target);os.replace(tmp,current)
link(digest)
if '--initial' in sys.argv:
 print('Initial certificate validated and copied');sys.exit(0)
try:
 subprocess.run(['/opt/private-surge/xray','run','-test','-config','/etc/private-surge/config.json'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 subprocess.run(['systemctl','restart','private-surge'],check=True)
 subprocess.run(['systemctl','is-active','--quiet','private-surge'],check=True)
except subprocess.CalledProcessError:
 if old:
  link(old);subprocess.run(['systemctl','restart','private-surge'],check=False)
 raise SystemExit('Surge certificate activation failed; previous certificate restored where available')
marker.write_text(digest);marker.chmod(0o600)
print('Surge certificate synchronized')
