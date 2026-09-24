import json,secrets,os,pathlib,hashlib
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
os.umask(0o077)
credentials=p/'credentials.json'
if not credentials.exists():
 credentials.write_text(json.dumps({'password':secrets.token_urlsafe(36),'token':secrets.token_urlsafe(32)}))
c=json.loads(credentials.read_text()); token=c['token']; password=c['password']
url=f'https://radar.yswdra.cn:25443/{token}/surge.conf'
profile=f'''#!MANAGED-CONFIG {url} interval=86400 strict=false
# Private Surge profile. Keep the profile and its URL private.
[General]
loglevel = notify
dns-server = system, 223.5.5.5, 119.29.29.29
skip-proxy = 127.0.0.1, localhost, *.local, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16

[Proxy]
Seoul = trojan, 43.155.203.253, 25443, password={password}, sni=radar.yswdra.cn, skip-cert-verify=false

[Proxy Group]
PROXY = select, Seoul

[Rule]
DOMAIN,radar.yswdra.cn,DIRECT
DOMAIN-SUFFIX,local,DIRECT
IP-CIDR,43.155.203.253/32,DIRECT,no-resolve
IP-CIDR,127.0.0.0/8,DIRECT,no-resolve
IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
IP-CIDR,172.16.0.0/12,DIRECT,no-resolve
IP-CIDR,192.168.0.0/16,DIRECT,no-resolve
IP-CIDR,169.254.0.0/16,DIRECT,no-resolve
IP-CIDR6,::1/128,DIRECT,no-resolve
IP-CIDR6,fc00::/7,DIRECT,no-resolve
IP-CIDR6,fe80::/10,DIRECT,no-resolve
GEOIP,CN,DIRECT
FINAL,PROXY

[Host]
radar.yswdra.cn = 43.155.203.253
'''
(p/'surge.conf').write_text(profile);(p/'subscription-url.txt').write_text(url+'\n')
blocked=['0.0.0.0/8','10.0.0.0/8','100.64.0.0/10','127.0.0.0/8','169.254.0.0/16','172.16.0.0/12','192.0.0.0/24','192.0.2.0/24','192.168.0.0/16','198.18.0.0/15','198.51.100.0/24','203.0.113.0/24','224.0.0.0/4','240.0.0.0/4','::/128','::1/128','64:ff9b::/96','100::/64','2001:db8::/32','fc00::/7','fe80::/10','ff00::/8','43.155.203.253/32']
server={'log':{'access':'none','loglevel':'warning'},'inbounds':[{'tag':'surge-trojan','listen':'0.0.0.0','port':25443,'protocol':'trojan','settings':{'clients':[{'password':password,'email':'surge-mac'}],'fallbacks':[{'dest':'127.0.0.1:25444'}]},'streamSettings':{'network':'tcp','security':'tls','tlsSettings':{'alpn':['http/1.1'],'minVersion':'1.2','certificates':[{'certificateFile':'/etc/private-surge/tls/current/fullchain.pem','keyFile':'/etc/private-surge/tls/current/key.pem','oneTimeLoading':True}]}}}], 'outbounds':[{'protocol':'freedom','tag':'direct','settings':{'domainStrategy':'UseIP'}},{'protocol':'blackhole','tag':'block'}],'routing':{'domainStrategy':'IPOnDemand','rules':[{'type':'field','domain':['localhost','domain:local','domain:internal','domain:localhost'],'outboundTag':'block'},{'type':'field','ip':blocked,'outboundTag':'block'}]},'policy':{'levels':{'0':{'handshake':10,'connIdle':300}}}}
(p/'server.json').write_text(json.dumps(server,indent=2))
headers={'Content-Type':['text/plain; charset=utf-8'],'Cache-Control':['private, no-store'],'X-Content-Type-Options':['nosniff'],'Referrer-Policy':['no-referrer']}
caddy={'admin':{'disabled':True},'logging':{'logs':{'default':{'level':'ERROR'}}},'apps':{'http':{'servers':{'subscription':{'listen':['127.0.0.1:25444'],'read_header_timeout':10000000000,'read_timeout':15000000000,'write_timeout':15000000000,'idle_timeout':15000000000,'max_header_bytes':16384,'routes':[{'match':[{'path':[f'/{token}/surge.conf'],'method':['GET','HEAD']}],'handle':[{'handler':'static_response','status_code':200,'headers':headers,'body':profile}], 'terminal':True},{'handle':[{'handler':'static_response','status_code':404,'body':'Not Found\n'}]}]}}}}}
(p/'profile-server.json').write_text(json.dumps(caddy))
client={'log':{'loglevel':'warning'},'inbounds':[{'listen':'127.0.0.1','port':19491,'protocol':'socks','settings':{'auth':'noauth','udp':True}}],'outbounds':[{'protocol':'trojan','settings':{'servers':[{'address':'43.155.203.253','port':25443,'password':password}]},'streamSettings':{'network':'tcp','security':'tls','tlsSettings':{'serverName':'radar.yswdra.cn','allowInsecure':False},'sockopt':{'interface':'en0'}}}]}
(p/'client-test.json').write_text(json.dumps(client))
print('Prepared private profile and independent service configs; secrets omitted')

(p/"sync-cert.py").write_bytes(Path(__file__).with_name("sync-cert.py").read_bytes())
