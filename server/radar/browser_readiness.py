"""Bounded article readiness on the already guarded page; never follows links."""

import asyncio
import time

SNAPSHOT = r"""() => {
 const visible = e => !!(e && e.getClientRects().length);
 const title = document.title.toLowerCase();
 const blocked = /just a moment|access denied|verify you|security verification/.test(title)
   || Array.from(document.querySelectorAll('input[type="password"],iframe[src*="challenges.cloudflare.com"],iframe[title*="challenge"]')).some(visible);
 const root = document.querySelector('article,main,[role="main"]') || document.body;
 if (!root) return {blocked, ready:false, signature:'', height:0};
 const nodes = Array.from(root.querySelectorAll('p,pre,li,td,h1,h2,h3')).slice(0,2000)
   .filter(e => visible(e) && !e.closest('nav,header,footer,aside,form,[contenteditable="true"]'));
 const text = nodes.map(e => e.innerText || '').join('\n').slice(0,80000);
 const busy = visible(root.closest('[aria-busy="true"]'))
   || Array.from(root.querySelectorAll('[aria-busy="true"],[role="progressbar"]')).some(visible);
 let hash = 2166136261;
 for (let i=0;i<text.length;i++) hash = Math.imul(hash ^ text.charCodeAt(i),16777619);
 return {blocked,ready:text.trim().length>=80 && !busy,signature:String(hash)+':'+text.length,
   height:document.documentElement.scrollHeight,viewport:innerHeight};
}"""


async def wait_for_content(page, *, timeout_seconds=12, stable_seconds=1.2, minimum_seconds=2.5):
    try:
        async with asyncio.timeout(timeout_seconds):
            return await _wait_for_content(page, timeout_seconds, stable_seconds, minimum_seconds)
    except TimeoutError:
        return {"timed_out": True, "blocked": False}


async def _wait_for_content(page, timeout_seconds, stable_seconds, minimum_seconds):
    start = time.monotonic()
    unchanged_at, signature, scrolls = start, None, 0
    while time.monotonic() - start < timeout_seconds:
        state = await page.evaluate(SNAPSHOT)
        if state.get("blocked"):
            return {"timed_out": False, "blocked": True}
        now = time.monotonic()
        if not state.get("ready") or state.get("signature") != signature:
            signature, unchanged_at = state.get("signature"), now
        if scrolls < 3 and state.get("height", 0) > state.get("viewport", 0) > 0:
            # Only reveal this article's lazy content. No buttons or links are clicked.
            await page.evaluate("() => window.scrollBy(0, Math.min(innerHeight * 0.8, 800))")
            scrolls += 1
            unchanged_at = now
        if state.get("ready") and now - start >= minimum_seconds and now - unchanged_at >= stable_seconds:
            return {"timed_out": False, "blocked": False}
        await asyncio.sleep(0.4)
    return {"timed_out": True, "blocked": False}
