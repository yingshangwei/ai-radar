#!/usr/bin/env python3
"""Install a reviewed noVNC 1.7.0 Android fix in a fresh cache namespace.

Backport https://github.com/novnc/noVNC/commit/7834e66 plus a rejection
handler so optional codec errors cannot block the remote-browser UI.
The original MPL-2.0 sources and notices are retained in the asset directory.
"""
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

NAMESPACE = 'android-compat-v1'
ORIGINAL = '592f56dc2195331c3b8329f8e51fa3a444a89ac85d5fa29e2364508aa9303df7'
BEFORE = 'supportsWebCodecsH264Decode = await _checkWebCodecsH264DecodeSupport();'
AFTER = '''// AI Radar: backport noVNC 7834e66 (MPL-2.0); optional codec detection
// must not block startup when Android VideoDecoder.flush() never settles.
_checkWebCodecsH264DecodeSupport().then((result) => {
    supportsWebCodecsH264Decode = result;
}).catch(() => {
    // Some older WebViews expose VideoDecoder without isConfigSupported.
    supportsWebCodecsH264Decode = false;
});'''


def install(root: Path):
    original = root / 'core/util/browser.js'
    source = original.read_bytes()
    if hashlib.sha256(source).hexdigest() != ORIGINAL:
        raise ValueError('Unexpected noVNC source; review upstream changes before patching')
    patched = source.decode().replace(BEFORE, AFTER)
    target = root / NAMESPACE
    if target.exists():
        for directory in ['core', 'vendor']:
            for source_file in (root / directory).rglob('*'):
                if not source_file.is_file():
                    continue
                relative = source_file.relative_to(root)
                expected = patched.encode() if source_file == original else source_file.read_bytes()
                if (target / relative).read_bytes() != expected:
                    raise ValueError('Existing compatibility assets differ; review before replacing')
        return target
    staging = Path(tempfile.mkdtemp(prefix='.novnc-compat-', dir=root))
    try:
        for directory in ['core', 'vendor']:
            shutil.copytree(root / directory, staging / directory)
        for filename in ['LICENSE.txt', 'AUTHORS']:
            if (root / filename).exists():
                shutil.copy2(root / filename, staging / filename)
        (staging / 'core/util/browser.js').write_text(patched)
        (staging / 'RADAR-PATCH.txt').write_text(__doc__)
        staging.chmod(0o755)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


if __name__ == '__main__':
    print(install(Path(sys.argv[1])))
