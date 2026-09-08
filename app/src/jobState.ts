import type { Job, Status } from "./types";

const stateLabels: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  retrying: "等待自动重试",
  needs_attention: "需要处理",
  completed: "已完成",
  failed: "失败",
};
const kindLabels: Record<string, string> = {
  collect: "信息采集",
  read: "网页解读",
  translate: "中文翻译",
  digest: "日报生成",
  daily: "采集与日报",
};
const phaseLabels: Record<string, string> = {
  queued: "等待执行",
  collect: "采集消息",
  translate: "翻译与校对",
  read: "读取与分析网页",
  presentation: "整理消息标题",
  digest: "生成与审核日报",
  completed: "处理完成",
  retry_wait: "等待重试",
  attention: "保留进度，等待处理",
  interrupted: "恢复中断的任务",
};
const stateOrder: Record<string, number> = {
  running: 0,
  retrying: 1,
  queued: 2,
  needs_attention: 3,
  failed: 4,
  completed: 4,
};
const validTime = (value?: string | null) => {
  const parsed = value ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : undefined;
};

/** Keep every returned active/attention job visible, followed by recent history. */
export function visibleJobs(jobs: Job[]): Job[] {
  const ordered = [...jobs].sort((a, b) => {
    const order = (stateOrder[a.status] ?? 3) - (stateOrder[b.status] ?? 3);
    if (order) return order;
    const stamp = (j: Job) =>
      validTime(
        ["completed", "failed"].includes(j.status)
          ? j.finished_at || j.started_at
          : j.queued_at || j.started_at,
      ) ?? 0;
    const byTime =
      a.status === "queued" && b.status === "queued"
        ? stamp(a) - stamp(b)
        : stamp(b) - stamp(a);
    return byTime || a.id.localeCompare(b.id);
  });
  return [
    ...ordered.filter((j) => !["completed", "failed"].includes(j.status)),
    ...ordered
      .filter((j) => ["completed", "failed"].includes(j.status))
      .slice(0, 3),
  ];
}

export function jobSummary(
  status: Pick<Status, "jobs" | "job_counts">,
): string {
  const counts =
    status.job_counts ??
    status.jobs.reduce<Record<string, number>>(
      (result, job) => ({
        ...result,
        [job.status]: (result[job.status] || 0) + 1,
      }),
      {},
    );
  const labels = ["running", "queued", "retrying", "needs_attention"]
    .filter((state) => Number.isInteger(counts[state]) && counts[state]! > 0)
    .map((state) => `${stateLabels[state]} ${counts[state]}`);
  return labels.join(" · ") || "暂无执行或排队中的任务";
}

export function jobView(job: Job, offline: boolean, serverNow?: string) {
  const phase = job.phase ? phaseLabels[job.phase] : undefined;
  const defaults: Record<string, string> = {
    queued: "已加入队列，会在前面的任务完成后自动执行。",
    running: phase
      ? `当前阶段：${phase}。`
      : "任务正在执行，等待下一次进度更新。",
    retrying: "已保存进度，服务器会按计划自动重试。",
    needs_attention: "进度已保留，部分工作需要进一步处理。",
    completed: "任务已完成。",
    failed: "本次任务未完成，已保存的结果仍会保留。",
  };
  const times: { label: string; value: string }[] = [];
  const add = (label: string, value?: string | null) => {
    if (validTime(value) !== undefined) times.push({ label, value: value! });
  };
  add("入队", job.queued_at);
  add("开始", job.started_at);
  add(offline ? "上次记录的进展" : "最近进展", job.progress_at);
  if (job.status === "running")
    add(offline ? "上次记录的响应" : "最近响应", job.heartbeat_at);
  if (job.status === "retrying")
    add(offline ? "上次重试计划" : "计划自动重试", job.retry_at);
  add("结束", job.finished_at);
  const heartbeat = validTime(job.heartbeat_at),
    reference = validTime(serverNow);
  // A heartbeat proves a response, never completed work; no local stale/failure inference.
  const responseNote =
    !offline &&
    job.status === "running" &&
    heartbeat !== undefined &&
    reference !== undefined &&
    reference >= heartbeat
      ? `截至本次状态更新，服务在 ${Math.floor((reference - heartbeat) / 1000)} 秒前响应。`
      : undefined;
  const attempt =
    Number.isInteger(job.attempt) && job.attempt! > 0
      ? `第 ${job.attempt}${Number.isInteger(job.max_attempts) && job.max_attempts! >= job.attempt! ? `/${job.max_attempts}` : ""} 次执行`
      : undefined;
  return {
    title: `${kindLabels[job.kind] || "后台任务"} · ${offline ? "上次状态：" : ""}${stateLabels[job.status] || "状态待确认"}`,
    message:
      job.message ||
      (offline
        ? "这是断网前保存的状态，当前进度尚未确认。"
        : defaults[job.status] || "等待服务器提供任务状态。"),
    phase:
      phase &&
      job.message &&
      !["queued", "completed", "retry_wait", "attention"].includes(job.phase!)
        ? `${offline ? "上次阶段" : "当前阶段"}：${phase}`
        : undefined,
    attempt,
    times,
    responseNote,
  };
}
