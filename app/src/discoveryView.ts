import type { Article, Status, Watch } from "./types";

const text = (value: unknown) =>
  typeof value === "string" ? value.trim() : "";
const validCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

/** Presentation only: model scores never become a probability of future popularity. */
export function earlySignalView(
  discovery: Article["discovery"],
  offline = false,
) {
  if (discovery?.kind !== "early_signal") return undefined;
  const initial = discovery.initial_engagement,
    current = discovery.current_engagement;
  const attention =
    discovery.outcome === "gaining_attention" &&
    validCount(initial) &&
    validCount(current) &&
    current > initial
      ? `${offline ? "上次记录：" : ""}已升温 · 综合互动 ${initial.toLocaleString("zh-CN")} → ${current.toLocaleString("zh-CN")}（+${(current - initial).toLocaleString("zh-CN")}）`
      : undefined;
  return {
    label: "潜力预判",
    reason: text(discovery.reason_zh) || "判断理由暂未同步。",
    uncertainty: text(discovery.uncertainty_zh) || "不确定点暂未同步。",
    attention,
    disclaimer: "当前为前瞻判断，不代表后续一定受到关注或取得预期结果。",
  };
}

export function trialWatchView(discovery: Watch["discovery"], offline = false) {
  if (discovery?.origin !== "automatic") return undefined;
  const labels = {
    trial: "自动试关注",
    retained: "自动发现 · 持续关注",
    expired: "自动试关注已到期",
    user_stopped: "自动发现 · 已停止关注",
  };
  const label = labels[discovery.status];
  if (!label) return undefined;
  const date = new Date(discovery.expires_at);
  const expiry =
    discovery.status === "trial"
      ? Number.isFinite(date.getTime())
        ? ` · 至 ${date.toLocaleDateString("zh-CN", { year: "numeric", month: "numeric", day: "numeric" })}`
        : " · 期限待同步"
      : "";
  return {
    label: `${offline ? "上次状态：" : ""}${label}${expiry}`,
    reason: text(discovery.reason_zh),
  };
}

export function discoveryStatusView(
  discovery: Status["discovery"],
  offline = false,
) {
  if (!discovery) return undefined;
  const prefix = offline ? "离线 · 上次状态：" : "";
  if (!discovery.enabled) return `${prefix}关联发现未开启`;
  const count = (value: unknown) =>
    validCount(value) ? value.toLocaleString("zh-CN") : "—";
  return `${prefix}待判断 ${count(discovery.pending)} · 待确认 ${count(discovery.unknown)} · 异常 ${count(discovery.error)}\n试关注 ${count(discovery.active_trials)} · 待了解账号 ${count(discovery.entities_pending)}\n${offline ? "上次记录的当日" : "今日"}判断调用 ${count(discovery.calls_today)} / ${count(discovery.daily_call_limit)}`;
}
