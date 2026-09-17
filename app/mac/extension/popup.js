import { config } from "./private-config.js";
const $ = (id) => document.getElementById(id);
async function render(action = "status") {
  const s = await chrome.runtime.sendMessage({ action });
  $("status").textContent =
    (s.message || (s.enabled ? "自动补采已开启" : "补采未启动")) +
    " · 已保存 " +
    (s.saved || 0) +
    " 页";
  $("waiting").replaceChildren();
  for (const job of s.waiting || []) {
    const a = document.createElement("a");
    a.textContent = "完成网站验证：" + job.domain;
    a.href = job.url;
    a.addEventListener("click", (e) => {
      e.preventDefault();
      chrome.tabs
        .update(job.tab, { active: true })
        .catch(() => chrome.tabs.create({ url: job.url }));
    });
    $("waiting").appendChild(a);
  }
}
$("domains").textContent = config.domains.join("、");
$("start").onclick = async () => {
  // Chrome requires this permission request to originate in a user gesture.
  const origins = config.domains.flatMap((h) => [
    "https://" + h + "/*",
    "https://www." + h + "/*",
    "http://" + h + "/*",
  ]);
  if (await chrome.permissions.request({ origins })) await render("start");
};
$("pause").onclick = () => render("pause");
$("resume").onclick = () => render("resume");
void render();
