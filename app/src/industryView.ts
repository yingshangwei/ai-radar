import type { Connection } from "./types";
import type {
  IndustryAssessment,
  IndustryEvidence,
  IndustryJob,
  IndustrySource,
  IndustryTheme,
} from "./industryTypes";

/** Credentials distinguish in-memory caches; never persist this key. */
export const industryQueryKey = (connection: Connection) =>
  ["industry", connection.url, connection.token, !!connection.demo] as const;

export function industryStateLabel(state: string) {
  return (
    (
      {
        improving: "改善迹象",
        mixed: "信号分化",
        weakening: "走弱迹象",
        insufficient_evidence: "证据不足",
      } as Record<string, string>
    )[state] || "状态待确认"
  );
}

export function assessmentStatusLabel(status?: string) {
  return (
    (
      {
        ready: "报告已生成",
        pending: "等待生成报告",
        calling: "正在生成或审核报告",
        needs_attention: "报告待处理",
        outcome_unknown: "生成结果待确认",
      } as Record<string, string>
    )[status || ""] || "尚无行业报告"
  );
}

export function visibleAssessment(theme: IndustryTheme): {
  assessment: IndustryAssessment | null;
  previous: boolean;
} {
  if (theme.assessment?.status === "ready")
    return { assessment: theme.assessment, previous: false };
  if (theme.previous_assessment?.status === "ready")
    return { assessment: theme.previous_assessment, previous: true };
  return { assessment: null, previous: false };
}

export function industryTime(value?: string | null, dateOnly = false) {
  if (!value || !Number.isFinite(Date.parse(value))) return "时间未提供";
  // A source that only supplies a calendar date has no known intraday timestamp.
  if (dateOnly && /^\d{4}-\d{2}-\d{2}/.test(value)) return value.slice(0, 10);
  return new Date(value).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function assessmentFreshness(asOf: string, now = Date.now()) {
  const time = Date.parse(asOf);
  if (!Number.isFinite(time)) return "报告截至时间未知";
  if (time > now + 5 * 60_000) return "报告时间晚于设备时间，请核对时钟";
  return now - time > 48 * 60 * 60_000
    ? "报告已超过 48 小时，可能未反映最新信息"
    : "报告截至时间在最近 48 小时内；结论仅覆盖此前的证据";
}

export function industrySourceLabel(source: IndustrySource, now = Date.now()) {
  const state =
    (
      {
        pending: "等待采集",
        healthy: "采集正常",
        error: "采集异常",
        disabled: "未启用",
      } as Record<string, string>
    )[source.status] || "状态未知";
  const success = source.last_success_at
    ? Date.parse(source.last_success_at)
    : NaN;
  if (
    source.status === "healthy" &&
    Number.isFinite(success) &&
    source.refresh_minutes > 0 &&
    now - success > source.refresh_minutes * 60_000
  )
    return "距上次成功已超过计划间隔";
  if (source.status === "healthy" && !Number.isFinite(success))
    return "成功时间未提供";
  return state;
}

export function industryJobActive(job?: IndustryJob | null) {
  return !!job && ["queued", "running", "retrying"].includes(job.status);
}

export function industryJobLabel(job: IndustryJob, stale = false) {
  const label =
    (
      {
        queued: "采集排队中",
        running: "正在采集",
        retrying: "等待自动重试",
        completed: "本轮采集已结束",
        failed: "本轮采集失败",
        needs_attention: "采集需要处理",
        outcome_unknown: "采集结果待确认",
      } as Record<string, string>
    )[job.status] || "采集状态待确认";
  return stale ? `上次状态：${label}` : label;
}

export function evidencePublishedLabel(evidence: IndustryEvidence) {
  if (!evidence.published_at) return "原始发布时间未提供";
  const dateOnly = ["date", "day"].includes(evidence.published_precision);
  const time = industryTime(evidence.published_at, dateOnly);
  if (time === "时间未提供") return "原始发布时间未提供";
  if (dateOnly) return `发布于 ${time}（仅日期，日内时间未知）`;
  if (
    !["second", "minute", "datetime", "timestamp", "exact"].includes(
      evidence.published_precision,
    )
  )
    return `来源标注时间 ${time}（精度未确认）`;
  return `发布于 ${time}`;
}

export function originalWebURL(value: string): string | null {
  try {
    const url = new URL(value.trim());
    if (
      !["https:", "http:"].includes(url.protocol) ||
      url.username ||
      url.password
    )
      return null;
    return url.toString();
  } catch {
    return null;
  }
}
