// Start before loading noVNC: optional browser feature detection must not hide
// failures or consume the short-lived ticket while dependency modules load.
(() => {
  const $ = id => document.getElementById(id);
  const base = location.pathname;
  const ticket = new URLSearchParams(location.hash.slice(1)).get('ticket');
  history.replaceState(null, '', base);
  let rfb, done = false, authenticated = false, failed = false;
  let connectTimer, expireTimer;
  const message = text => { $('status').textContent = text; };
  const notify = data => {
    if (window.ReactNativeWebView) window.ReactNativeWebView.postMessage(JSON.stringify(data));
  };
  const fail = text => {
    if (done) return;
    failed = true;
    clearTimeout(connectTimer);
    $('finish').disabled = true;
    message(text);
    $('screen').setAttribute('aria-label', text);
    if (!authenticated) $('domain').textContent = '连接未完成';
  };
  const deadline = (promise, ms, text) => {
    let timer;
    return Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(text)), ms);
    })]).finally(() => clearTimeout(timer));
  };
  async function request(action, body = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), action === 'finish' ? 90000 : 15000);
    try {
      const response = await fetch(base + action, {method:'POST', credentials:'same-origin',
        signal:controller.signal, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '连接失败，请关闭后从 App 重新打开。');
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('连接请求超时，请检查网络并关闭窗口后重新打开。');
      throw error;
    } finally { clearTimeout(timer); }
  }
  $('finish').disabled = true;
  $('finish').onclick = async () => {
    $('finish').disabled = true;
    message('正在返回目标文章验证读取结果，请稍候…');
    try {
      const result = await request('finish');
      message(result.message);
      if (result.ready) {
        done = true; clearTimeout(expireTimer); rfb.disconnect();
        $('close').textContent = '返回 App'; notify({type:'complete'});
      }
    } catch (error) { message(error.message); }
    finally { $('finish').disabled = done || failed; }
  };
  $('close').onclick = async () => {
    done = true; clearTimeout(connectTimer); clearTimeout(expireTimer);
    if (rfb) rfb.disconnect();
    try { if (authenticated) await request('close'); } catch (_) {}
    $('finish').disabled = true;
    message('授权窗口已关闭，登录状态保留。'); notify({type:'close'});
  };
  const key = (sym, code) => { if (rfb && !failed) rfb.sendKey(sym, code); };
  $('tab').onclick = () => key(0xff09, 'Tab');
  $('backspace').onclick = () => key(0xff08, 'Backspace');
  $('enter').onclick = () => key(0xff0d, 'Enter');
  $('keyboard').onclick = () => {
    const input = $('input'); input.hidden = !input.hidden;
    if (!input.hidden) input.focus();
  };
  // Text goes only through encrypted RFB events, never application logs/storage.
  $('input').addEventListener('input', event => {
    if (event.isComposing) return;
    for (const ch of $('input').value) {
      const cp = ch.codePointAt(0); key(cp <= 255 ? cp : 0x01000000 | cp);
    }
    $('input').value = '';
  });
  $('input').addEventListener('keydown', event => {
    if (event.key === 'Backspace' && !event.target.value) {
      event.preventDefault(); key(0xff08, 'Backspace');
    }
    if (event.key === 'Enter') key(0xff0d, 'Enter');
  });
  $('input').addEventListener('beforeinput', event => {
    if (event.inputType === 'deleteContentBackward' && !event.target.value) {
      event.preventDefault(); key(0xff08, 'Backspace');
    }
  });
  window.addEventListener('error', () => fail('网页组件运行失败，请关闭窗口后重新打开。'));
  window.addEventListener('unhandledrejection', () => fail('网页组件运行失败，请关闭窗口后重新打开。'));
  async function start() {
    try {
      if (!ticket) throw new Error('此入口已失效，请从 App 重新打开。');
      $('domain').textContent = '建立安全连接…';
      message('正在验证授权窗口…');
      const session = await request('exchange', {ticket});
      authenticated = true;
      if (done) { await request('close'); return; }
      $('domain').textContent = session.domain;
      $('time').textContent = '窗口有效至 ' + new Date(session.expires_at * 1000).toLocaleTimeString();
      expireTimer = setTimeout(() => {
        fail('窗口已过期，请返回授权中心重新打开。'); if (rfb) rfb.disconnect();
      }, Math.max(0, session.expires_at * 1000 - Date.now()));
      message('正在加载远程浏览器组件…');
      const module = await deadline(import('/v1/browser/novnc/android-compat-v1/core/rfb.js'), 45000,
        '网页组件加载超时，请关闭窗口后重开；若仍失败，请切换网络重试。');
      if (done || failed) return;
      message('正在连接远程画面…');
      rfb = new module.default($('screen'),
        `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${base}socket`);
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.showDotCursor = true;
      connectTimer = setTimeout(() => {
        fail('远程画面连接超时，请关闭窗口后重开，或切换网络重试。'); rfb.disconnect();
      }, 25000);
      rfb.addEventListener('connect', () => {
        clearTimeout(connectTimer);
        if (done || failed) return;
        $('finish').disabled = false;
        message('已连接。完成网站登录或验证后，点击“验证并补采”。');
      });
      rfb.addEventListener('disconnect', () => {
        if (!done && !failed) fail('远程画面已断开，请关闭窗口后重新打开；网站登录状态仍保留。');
      });
      rfb.addEventListener('securityfailure', () => fail('远程画面连接校验失败，请关闭窗口后重新打开。'));
    } catch (error) {
      fail(error.message || '连接失败，请关闭窗口后重新打开。');
    }
  }
  void start();
})();
