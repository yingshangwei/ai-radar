import { config } from "./private-config.js";
import {
  identity,
  permitted,
  deferred,
  backoff,
  readyDomains,
} from "./policy.mjs";
let busy = false;
const load = async () => {
  const { state, control } = await chrome.storage.local.get([
    "state",
    "control",
  ]);
  return {
    ...(state || {
      device: crypto.randomUUID(),
      saved: 0,
      failures: {},
      waiting: [],
    }),
    enabled: control?.enabled || false,
    domains: control?.domains || [],
  };
};
const save = async (state) => {
  // Control state is separate: a delayed network response cannot undo Pause.
  const { enabled, domains, ...data } = state;
  await chrome.storage.local.set({ state: data });
};
async function api(path, data) {
  const response = await fetch(config.server + "/v1/companion" + path, {
    method: data ? "POST" : "GET",
    credentials: "omit",
    redirect: "error",
    cache: "no-store",
    signal: AbortSignal.timeout(25000),
    headers: {
      Authorization: "Bearer " + config.token,
      ...(data ? { "Content-Type": "application/json" } : {}),
    },
    ...(data ? { body: JSON.stringify(data) } : {}),
  });
  if (!response.ok) throw Error("服务器返回 " + response.status);
  return response.json();
}
async function report(s, state) {
  await api("/heartbeat", {
    device: s.device,
    state,
    saved: s.saved,
    domains: s.domains,
    waiting: s.waiting.length,
  });
}
async function capture(s) {
  const job = s.active;
  let tab;
  try {
    tab = await chrome.tabs.get(job.tab);
  } catch {
    throw Error("采集标签页已关闭");
  }
  if (
    !permitted(tab.url, s.domains) ||
    identity(tab.url) !== identity(job.url)
  ) {
    const e = Error("页面跳转或等待登录，请在 Mac 查看");
    e.verification = true;
    throw e;
  }
  if (tab.status !== "complete") {
    if (Date.now() - job.started > 90_000)
      throw Error("网页加载超时，已保留稍后重试");
    return false;
  }
  const results = await chrome.scripting.executeScript({
    target: { tabId: job.tab, allFrames: false },
    world: "ISOLATED",
    files: ["capture.js"],
  });
  const data = results[0]?.result;
  if (!data || data.error) {
    const e = Error(
      data?.error === "verification_required"
        ? "需要在 Mac 完成网站验证"
        : "没有读到正文，已保留重试",
    );
    e.verification = data?.error === "verification_required";
    throw e;
  }
  if (
    identity(data.url) !== identity(job.url) ||
    !permitted(data.url, s.domains)
  )
    throw Error("正文与目标文章不匹配");
  // Whitelist data fields. Credentials never enter the page/content script.
  if (!(await load()).enabled) return false;
  const receipt = await api("/mobile-import", {
    document_id: job.document_id,
    method: "mac_browser",
    url: data.url,
    title: data.title,
    text: data.text,
    links: data.links,
    partial: data.partial,
  });
  if (!receipt.already_saved) s.saved++;
  delete s.failures[job.document_id];
  s.waiting = s.waiting.filter((x) => x.document_id !== job.document_id);
  s.active = null;
  await save(s); // Persist success before closing only the tab this extension created.
  await chrome.tabs.remove(job.tab).catch(() => {});
  return true;
}
async function tick() {
  if (busy) return;
  busy = true;
  let s;
  try {
    s = await load();
    if (!s.enabled) {
      await report(s, "paused");
      return;
    }
    const { resumeRequested } =
      await chrome.storage.local.get("resumeRequested");
    if (resumeRequested && !s.active) {
      const index =
        typeof resumeRequested === "string"
          ? s.waiting.findIndex((j) => j.document_id === resumeRequested)
          : 0;
      const job = index < 0 ? undefined : s.waiting.splice(index, 1)[0];
      if (job) {
        s.active = { ...job, started: Date.now() };
        delete s.failures[job.document_id];
      }
      await chrome.storage.local.remove("resumeRequested");
      await save(s);
    }
    if (s.active) {
      await report(s, "working");
      try {
        if (!(await capture(s))) return;
      } catch (e) {
        const job = s.active;
        if (job) {
          s.failures[job.document_id] = backoff(
            s.failures[job.document_id],
            !!e.verification,
          );
          if (e.verification) {
            s.waiting = [
              ...s.waiting.filter((x) => x.domain !== job.domain),
              job,
            ].slice(-30);
          } else await chrome.tabs.remove(job.tab).catch(() => {});
          s.active = null;
        }
        s.message = e.message;
        await save(s);
        await report(s, e.verification ? "verification" : "error");
        return;
      }
    }
    const excluded = deferred(s.failures);
    const domains = readyDomains(s.domains, s.waiting);
    const queue = domains.length
      ? await api(
          "/mobile-queue?limit=3&domains=" +
            encodeURIComponent(domains.join(",")) +
            "&exclude_document_ids=" +
            encodeURIComponent(excluded.join(",")),
        )
      : [];
    const job = queue.find(
      (j) =>
        permitted(j.url, s.domains) &&
        !s.waiting.some((w) => w.domain === j.domain),
    );
    s.message = job
      ? "正在用 Mac 补采"
      : s.waiting.length
        ? "有网站等待一次验证；其他网站继续自动采集"
        : "已连接，自动补采中";
    if (job) {
      const allowed = await chrome.permissions.contains({
        origins: [new URL(job.url).origin + "/*"],
      });
      if (!allowed) {
        s.message = "请在扩展中重新授予网站读取权限";
        await chrome.storage.local.set({
          control: { enabled: false, domains: s.domains },
        });
      } else {
        if (!(await load()).enabled) return;
        const tab = await chrome.tabs.create({ url: job.url, active: false });
        s.active = { ...job, tab: tab.id, started: Date.now() };
      }
    }
    await save(s);
    await report(
      s,
      s.active ? "working" : s.waiting.length ? "verification" : "ready",
    );
  } catch (e) {
    if (s) {
      s.message = "连接暂不可用：" + e.message;
      await save(s);
    }
  } finally {
    busy = false;
  }
}
async function init() {
  // An alarm may be removed at browser restart. Recreate it on every startup.
  await chrome.storage.local.setAccessLevel({
    accessLevel: "TRUSTED_CONTEXTS",
  });
  await chrome.alarms.create("radar-collect", { periodInMinutes: 0.5 });
  await tick();
}
chrome.runtime.onInstalled.addListener(init);
chrome.runtime.onStartup.addListener(init);
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "radar-collect") void tick();
});
chrome.tabs.onUpdated.addListener((id, info, tab) => {
  if (info.status !== "complete") return;
  void (async () => {
    const s = await load();
    const job = s.waiting.find(
      (j) => j.tab === id && identity(j.url) === identity(tab.url),
    );
    if (s.enabled && job)
      await chrome.storage.local.set({ resumeRequested: job.document_id });
    await tick();
  })();
});
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || sender.tab) return false;
  void (async () => {
    const s = await load();
    if (message.action === "start") {
      await chrome.storage.local.set({
        control: { enabled: true, domains: config.domains },
      });
    } else if (message.action === "pause") {
      await chrome.storage.local.set({
        control: { enabled: false, domains: s.domains },
      });
    } else if (message.action === "resume") {
      await chrome.storage.local.set({ resumeRequested: true });
    }
    respond(await load());
    if (message.action !== "status") void tick();
  })().catch(() => respond({ message: "暂时无法读取状态" }));
  return true;
});
