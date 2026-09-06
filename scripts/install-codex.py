#!/usr/bin/env python3
"""Install the official pinned standalone Codex binary after checking npm SHA-512 integrity."""
import argparse
import base64
import hashlib
import io
import json
import os
import tarfile
import urllib.request
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--version',default='0.153.3')
parser.add_argument('--destination',type=Path,default=Path('/opt/ai-radar/tools/bin'))
args=parser.parse_args()
if any(c not in '0123456789.' for c in args.version):
    parser.error('Expected a pinned numeric release version')
metadata=json.load(urllib.request.urlopen(f'https://registry.npmjs.org/@openai/codex/{args.version}-linux-x64',timeout=30))
url=metadata['dist']['tarball']
if not url.startswith('https://registry.npmjs.org/@openai/codex/'):
    raise RuntimeError('Unexpected package origin')
with urllib.request.urlopen(url,timeout=120) as response:
    data=response.read(250_000_000)
actual='sha512-'+base64.b64encode(hashlib.sha512(data).digest()).decode()
if actual!=metadata['dist']['integrity']:
    raise RuntimeError('Package integrity mismatch')
with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
    members=[m for m in archive.getmembers() if m.name.endswith('/codex/codex') and m.isfile()]
    if len(members)!=1:
        raise RuntimeError('Unexpected native package layout')
    args.destination.mkdir(parents=True,exist_ok=True)
    target=args.destination/'codex'
    temporary=args.destination/'codex.new'
    with archive.extractfile(members[0]) as source,temporary.open('wb') as dest:
        import shutil
        shutil.copyfileobj(source,dest)
    temporary.chmod(0o755)
    os.replace(temporary,target)
print('Installed verified official Codex',args.version,'at',target)
