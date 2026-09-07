import RFB from '/v1/browser/novnc/core/rfb.js';
const $ = id => document.getElementById(id);
const base = location.pathname;
const ticket = new URLSearchParams(location.hash.slice(1)).get('ticket');
history.replaceState(null, '', base);
let rfb, done = false;
const message = text => { $('status').textContent = text; };
const notify = data => window.ReactNativeWebView?.postMessage(JSON.stringify(data));
async function request(action, body = {}) {
  const response = await fetch(base + action, {method:'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || '连接失败，请关闭后从 App 重新打开。');
  return data;
}
try {
  if (!ticket) throw new Error('此入口已失效，请从 App 重新打开。');
  const session = await request('exchange', {ticket});
  $('domain').textContent = session.domain;
  $('time').textContent = '窗口有效至 ' + new Date(session.expires_at * 1000).toLocaleTimeString();
  rfb = new RFB($('screen'), `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${base}socket`);
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.showDotCursor = true;
  rfb.addEventListener('disconnect', () => {if (!done) message('浏览器连接已断开；可尝试验证，或关闭窗口后重新打开。');});
  setTimeout(() => {if (!done) {rfb.disconnect(); message('窗口已过期，请返回授权中心重新打开。');}},
    Math.max(0, session.expires_at * 1000 - Date.now()));
} catch (error) {message(error.message); $('finish').disabled = true;}
$('finish').onclick = async () => {
  $('finish').disabled = true;
  message('正在返回目标文章验证读取结果，请稍候…');
  try {
    const result = await request('finish');
    message(result.message);
    if (result.ready) {done = true; rfb?.disconnect(); $('close').textContent = '返回 App'; notify({type:'complete'});}
  } catch(error) {message(error.message);}
  finally {$('finish').disabled = done;}
};
$('close').onclick = async () => {
  try {if (!done) await request('close');} catch (_) {}
  done = true; rfb?.disconnect(); $('finish').disabled = true;
  message('授权窗口已关闭，登录状态保留。'); notify({type:'close'});
};
const key = (sym, code) => rfb?.sendKey(sym, code);
$('tab').onclick = () => key(0xff09, 'Tab');
$('enter').onclick = () => key(0xff0d, 'Enter');
$('keyboard').onclick = () => {const input=$('input'); input.hidden=!input.hidden; if (!input.hidden) input.focus();};
// Send text through RFB keyboard events; never submit it to an application API,
// log it, or keep passwords in page storage. Supports mobile IME composition.
$('input').addEventListener('input', event => {
  if (event.isComposing) return;
  const input=$('input');
  for (const ch of input.value) {
    const cp=ch.codePointAt(0); key(cp <= 255 ? cp : 0x01000000 | cp);
  }
  input.value='';
});
$('input').addEventListener('keydown', event => {
  if (event.key === 'Backspace' && !event.target.value) key(0xff08, 'Backspace');
  if (event.key === 'Enter') key(0xff0d, 'Enter');
});
