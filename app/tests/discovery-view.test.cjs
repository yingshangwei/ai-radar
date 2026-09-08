const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const ts = require("typescript");
const source = ts.transpileModule(
  readFileSync(resolve(__dirname, "../src/discoveryView.ts"), "utf8"),
  {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  },
).outputText;
const mod = { exports: {} };
vm.runInThisContext(`(function(exports){${source}\n})`)(mod.exports);
const { earlySignalView, trialWatchView, discoveryStatusView } = mod.exports;
const signal = (extra = {}) => ({
  kind: "early_signal",
  label: "潜力预判",
  reason_zh: "公开了可验证的初步研究与实现。",
  uncertainty_zh: "仍需独立复现，尚无实际部署证据。",
  confidence: 0.87,
  potential_impact: 8,
  novelty: 9,
  judged_at: "2026-09-08T08:00:00Z",
  initial_engagement: 0,
  current_engagement: 0,
  outcome: "pending",
  ...extra,
});

test("early-signal copy preserves server reasons and uncertainties without publishing score probabilities", () => {
  const data = signal(),
    before = JSON.stringify(data),
    view = earlySignalView(data);
  assert.equal(view.label, "潜力预判");
  assert.equal(view.reason, data.reason_zh);
  assert.equal(view.uncertainty, data.uncertainty_zh);
  assert.match(view.disclaimer, /当前为前瞻判断/);
  assert.equal(view.attention, undefined);
  assert.doesNotMatch(
    JSON.stringify(view),
    /87|0\.87|confidence|potential_impact|novelty|%/,
  );
  assert.equal(JSON.stringify(data), before);
  assert.equal(earlySignalView(undefined), undefined);
  assert.equal(earlySignalView({ kind: "unknown" }), undefined);
});

test("gaining attention needs the explicit outcome and real nonnegative interaction growth", () => {
  assert.equal(
    earlySignalView(
      signal({ outcome: "gaining_attention", current_engagement: 31 }),
    ).attention,
    "已升温 · 综合互动 0 → 31（+31）",
  );
  assert.equal(
    earlySignalView(
      signal({
        outcome: "gaining_attention",
        initial_engagement: 3,
        current_engagement: 41,
      }),
      true,
    ).attention,
    "上次记录：已升温 · 综合互动 3 → 41（+38）",
  );
  assert.equal(
    earlySignalView(signal({ current_engagement: 1000 })).attention,
    undefined,
  );
  for (const [initial, current] of [
    [30, 30],
    [30, 20],
    [-1, 30],
    [0, NaN],
    [0, Infinity],
    [0, 1.5],
    [0, "31"],
  ]) {
    assert.equal(
      earlySignalView(
        signal({
          outcome: "gaining_attention",
          initial_engagement: initial,
          current_engagement: current,
        }),
      ).attention,
      undefined,
    );
  }
});

test("automatic watches show server lifecycle and expiry without changing or guessing switch state", () => {
  const data = {
    origin: "automatic",
    status: "trial",
    expires_at: "2026-09-15T12:00:00Z",
    reason_zh: "被已关注研究者引用，持续发布原创研究。",
  };
  const before = JSON.stringify(data),
    view = trialWatchView(data);
  assert.match(view.label, /^自动试关注 · 至 2026\/9\/15$/);
  assert.equal(view.reason, data.reason_zh);
  assert.equal(JSON.stringify(data), before);
  assert.match(
    trialWatchView({ ...data, expires_at: "invalid" }).label,
    /期限待同步/,
  );
  assert.equal(
    trialWatchView({ ...data, status: "expired" }).label,
    "自动试关注已到期",
  );
  assert.equal(
    trialWatchView({ ...data, status: "user_stopped" }, true).label,
    "上次状态：自动发现 · 已停止关注",
  );
  assert.equal(
    trialWatchView({ ...data, status: "retained" }).label,
    "自动发现 · 持续关注",
  );
  assert.equal(trialWatchView(undefined), undefined);
  assert.equal(trialWatchView({ ...data, origin: "manual" }), undefined);
});

test("discovery status supports old servers, offline snapshots and missing counts without fabricating zeros", () => {
  const data = {
    enabled: true,
    pending: 2,
    unknown: 1,
    error: 0,
    active_trials: 3,
    entities_pending: 4,
    calls_today: 5,
    daily_call_limit: 10,
  };
  assert.equal(discoveryStatusView(undefined), undefined);
  assert.equal(discoveryStatusView({ enabled: false }), "关联发现未开启");
  assert.equal(
    discoveryStatusView(data),
    "待判断 2 · 待确认 1 · 异常 0\n试关注 3 · 待了解账号 4\n今日判断调用 5 / 10",
  );
  const offline = discoveryStatusView(data, true);
  assert.match(offline, /^离线 · 上次状态/);
  assert.match(offline, /上次记录的当日判断调用/);
  assert.doesNotMatch(offline, /今日|正在|%/);
  assert.match(
    discoveryStatusView({ ...data, pending: undefined }),
    /待判断 —/,
  );
});
