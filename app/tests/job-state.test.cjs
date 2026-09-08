const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const ts = require("typescript");
const source = ts.transpileModule(
  readFileSync(resolve(__dirname, "../src/jobState.ts"), "utf8"),
  {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  },
).outputText;
const mod = { exports: {} };
vm.runInThisContext(`(function(exports){${source}\n})`)(mod.exports);
const { visibleJobs, jobSummary, jobView } = mod.exports;
const stamp = "2026-09-08T05:00:00Z";
const job = (id, status, extra = {}) => ({
  id,
  kind: "read",
  status,
  message: "",
  queued_at: stamp,
  started_at: null,
  ...extra,
});

test("a long-running job stays first ahead of eleven later queued jobs and recent history", () => {
  const jobs = [
    ...Array.from({ length: 11 }, (_, i) =>
      job(`queued-${i}`, "queued", {
        queued_at: `2026-09-08T06:${String(i).padStart(2, "0")}:00Z`,
      }),
    ),
    job("old-running", "running", { queued_at: stamp, started_at: stamp }),
    job("retry", "retrying"),
    job("attention", "needs_attention"),
    ...Array.from({ length: 5 }, (_, i) =>
      job(`done-${i}`, "completed", { finished_at: `2026-09-08T07:0${i}:00Z` }),
    ),
  ];
  const before = JSON.stringify(jobs),
    shown = visibleJobs(jobs);
  assert.equal(shown[0].id, "old-running");
  assert.equal(shown[1].id, "retry");
  assert.deepEqual(
    shown.filter((j) => j.status === "queued").map((j) => j.id),
    Array.from({ length: 11 }, (_, i) => `queued-${i}`),
  );
  assert.equal(shown.filter((j) => j.status === "needs_attention").length, 1);
  assert.deepEqual(
    shown.filter((j) => j.status === "completed").map((j) => j.id),
    ["done-4", "done-3", "done-2"],
  );
  assert.equal(JSON.stringify(jobs), before);
});

test("global queue counts are used even if the returned list is bounded, with legacy fallback", () => {
  assert.equal(
    jobSummary({
      jobs: [job("q", "queued")],
      job_counts: {
        running: 1,
        queued: 11,
        retrying: 2,
        needs_attention: 3,
        completed: 100,
      },
    }),
    "执行中 1 · 排队中 11 · 等待自动重试 2 · 需要处理 3",
  );
  assert.equal(jobSummary({ jobs: [job("old", "running")] }), "执行中 1");
  assert.equal(jobSummary({ jobs: [] }), "暂无执行或排队中的任务");
});

test("states and phases use Chinese labels, queued jobs do not claim they are executing", () => {
  const labels = {
    queued: "排队中",
    running: "执行中",
    retrying: "等待自动重试",
    needs_attention: "需要处理",
    completed: "已完成",
    failed: "失败",
  };
  for (const [state, label] of Object.entries(labels)) {
    const view = jobView(job(state, state), false);
    assert.equal(view.title, `网页解读 · ${label}`);
    assert.ok(view.message);
  }
  assert.match(jobView(job("q", "queued"), false).message, /前面的任务完成后/);
  const view = jobView(
    job("r", "running", {
      kind: "translate",
      phase: "translate",
      attempt: 2,
      max_attempts: 3,
    }),
    false,
  );
  assert.match(view.message, /翻译与校对/);
  assert.equal(view.attempt, "第 2/3 次执行");
  const unknown = jobView(
    job("u", "unknown-secret", {
      kind: "untrusted-kind",
      phase: "untrusted-phase",
      reason_code: "untrusted-reason",
    }),
    false,
  );
  assert.equal(unknown.title, "后台任务 · 状态待确认");
  assert.doesNotMatch(JSON.stringify(unknown), /untrusted|unknown-secret/);
});

test("heartbeat and progress remain separate and server time does not fabricate completion", () => {
  const view = jobView(
    job("r", "running", {
      heartbeat_at: "2026-09-08T06:00:00Z",
      progress_at: stamp,
    }),
    false,
    "2026-09-08T06:00:30Z",
  );
  assert.equal(view.times.find((t) => t.label === "最近进展").value, stamp);
  assert.equal(
    view.times.find((t) => t.label === "最近响应").value,
    "2026-09-08T06:00:00Z",
  );
  assert.match(view.responseNote, /30 秒前响应/);
  assert.doesNotMatch(view.title, /已完成|失败/);
  assert.doesNotMatch(JSON.stringify(view), /%/);
  assert.equal(
    jobView(
      job("r", "running", { heartbeat_at: stamp }),
      false,
      "2026-09-08T04:00:00Z",
    ).responseNote,
    undefined,
  );
});

test("offline snapshots never claim current liveness or an upcoming automatic retry", () => {
  const view = jobView(
    job("r", "running", {
      heartbeat_at: stamp,
      progress_at: stamp,
      phase: "read",
      message: "已保存一阶段结果。",
    }),
    true,
    "2026-09-08T06:00:00Z",
  );
  assert.match(view.title, /上次状态/);
  assert.equal(view.phase, "上次阶段：读取与分析网页");
  assert.equal(view.responseNote, undefined);
  assert.ok(view.times.some((t) => t.label === "上次记录的响应"));
  const retry = jobView(job("t", "retrying", { retry_at: stamp }), true);
  assert.match(retry.message, /当前进度尚未确认/);
  assert.ok(retry.times.some((t) => t.label === "上次重试计划"));
  const legacy = jobView(
    {
      id: "old",
      kind: "daily",
      status: "completed",
      message: "已有结果",
      started_at: stamp,
    },
    false,
  );
  assert.equal(legacy.message, "已有结果");
  assert.equal(legacy.times.length, 1);
  assert.equal(
    jobView(
      job("bad", "running", { started_at: "invalid", heartbeat_at: "invalid" }),
      false,
    ).times.length,
    1,
  );
});

test("discovery task and phase have clear labels without invented prediction percentages", () => {
  const view = jobView(
    job("discover", "running", { kind: "discover", phase: "discover" }),
    false,
  );
  assert.equal(view.title, "关联发现与潜力判断 · 执行中");
  assert.equal(view.message, "当前阶段：关联发现与潜力判断。");
  assert.doesNotMatch(JSON.stringify(view), /%|必火|概率/);
  const offline = jobView(
    job("discover", "running", {
      kind: "discover",
      phase: "discover",
      message: "已保存判断。",
    }),
    true,
  );
  assert.equal(offline.title, "关联发现与潜力判断 · 上次状态：执行中");
  assert.equal(offline.phase, "上次阶段：关联发现与潜力判断");
});
