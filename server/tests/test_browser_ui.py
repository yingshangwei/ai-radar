"""Browser regressions. Run with RADAR_TEST_CHROMIUM and RADAR_TEST_NOVNC set."""
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytest.importorskip('playwright.async_api')
from playwright.async_api import async_playwright  # noqa: E402

CHROME = os.environ.get('RADAR_TEST_CHROMIUM')
ASSETS = os.environ.get('RADAR_TEST_NOVNC')
pytestmark = pytest.mark.skipif(not CHROME or not ASSETS, reason='Explicit browser fixtures not configured')
UI = Path(__file__).parents[1] / 'radar/browser_ui'


@pytest.fixture
async def page():
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME)
        page = await browser.new_page(viewport={'width': 412, 'height': 720})
        yield page
        await browser.close()


@pytest.mark.parametrize('fault', ['hang', 'missing', 'reject'])
async def test_codec_probe_never_blocks_module_loading(page, fault):
    async def serve(route):
        path = urlsplit(route.request.url).path.lstrip('/')
        if not path:
            await route.fulfill(body='<html><body>fixture</body></html>', content_type='text/html')
        else:
            await route.fulfill(path=Path(ASSETS) / path, content_type='text/javascript')
    await page.route('https://radar.test/**', serve)
    faults = {
        'hang': '''window.VideoDecoder=class {static async isConfigSupported(){return {supported:true}}
            configure(){} decode(){} flush(){return new Promise(()=>{})}};
            window.EncodedVideoChunk=class {};''',
        'missing': 'window.VideoDecoder=class {};',
        'reject': '''window.VideoDecoder=class {
            static async isConfigSupported(){throw new Error('unsupported hardware')}};''',
    }
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    await page.add_init_script(faults[fault])
    await page.goto('https://radar.test/')
    await page.evaluate("""() => {
        window.oldLoaded=false; window.fixedLoaded=false;
        import('/core/util/browser.js').then(()=>window.oldLoaded=true).catch(()=>{});
        import('/android-compat-v1/core/util/browser.js').then(()=>window.fixedLoaded=true);
    }""")
    await page.wait_for_function('window.fixedLoaded === true', timeout=3000)
    assert not await page.evaluate('window.oldLoaded')
    assert not errors


async def viewer(page, module, *, stalled_exchange=False):
    calls = []
    async def serve(route):
        path = urlsplit(route.request.url).path
        calls.append(path)
        if path.endswith('/exchange'):
            await route.fulfill(json={'domain': 'example.org', 'expires_at': time.time() + 1200})
        elif path.endswith('/close'):
            await route.fulfill(json={'closed': True})
        elif '/assets/' in path:
            name = path.rsplit('/', 1)[1]
            await route.fulfill(path=UI / name,
                                content_type='text/css' if name.endswith('.css') else 'text/javascript')
        elif path.endswith('/core/rfb.js'):
            await route.fulfill(body=module, content_type='text/javascript')
        else:
            await route.fulfill(path=UI / 'index.html', content_type='text/html')
    await page.route('https://radar.test/**', serve)
    await page.clock.install()
    if stalled_exchange:
        await page.add_init_script('''window.fetch=(url,init)=>new Promise((resolve,reject)=>{
            init.signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')));
        });''')
    await page.goto('https://radar.test/v1/browser/view/test/#ticket=' + 't' * 43)
    return calls


async def test_module_load_timeout_shows_error_after_early_ticket_exchange(page):
    calls = await viewer(page, 'await new Promise(()=>{}); export default class {};')
    await page.wait_for_function("document.querySelector('#status').textContent.includes('加载远程')")
    assert calls.index('/v1/browser/view/test/exchange') < calls.index(
        '/v1/browser/novnc/android-compat-v1/core/rfb.js')
    await page.clock.fast_forward(46000)
    assert '网页组件加载超时' in await page.locator('#status').inner_text()
    assert await page.locator('#finish').is_disabled()
    await page.locator('#close').click()
    assert '/v1/browser/view/test/close' in calls


async def test_ticket_exchange_timeout_does_not_remain_connecting(page):
    await viewer(page, '', stalled_exchange=True)
    await page.wait_for_function("document.querySelector('#domain').textContent.includes('安全连接')")
    await page.clock.fast_forward(16000)
    assert '连接请求超时' in await page.locator('#status').inner_text()
    assert await page.locator('#domain').inner_text() == '连接未完成'
    assert await page.locator('#finish').is_disabled()


async def test_vnc_handshake_timeout_is_visible(page):
    await viewer(page, 'export default class extends EventTarget {disconnect(){}}')
    await page.wait_for_function("document.querySelector('#status').textContent.includes('连接远程画面')")
    await page.clock.fast_forward(26000)
    assert '远程画面连接超时' in await page.locator('#status').inner_text()
    assert await page.locator('#finish').is_disabled()
