const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const ts = require("typescript");

function load(path, mocks = {}) {
  const filename = resolve(__dirname, path);
  const source = ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const module = { exports: {} };
  vm.runInThisContext(`(function(require,module,exports){${source}\n})`, {
    filename,
  })(
    (name) => {
      if (!(name in mocks)) throw new Error(`Unexpected import ${name}`);
      return mocks[name];
    },
    module,
    module.exports,
  );
  return module.exports;
}

const view = load("../src/industryView.ts");

test("a new pending or unknown assessment never publishes its draft over a ready history", () => {
  const previous = { id: "old", status: "ready", summary_zh: "已完成报告" };
  for (const status of ["pending", "needs_attention", "outcome_unknown"]) {
    const draft = { id: "new", status, summary_zh: "不应发布的未完成草稿" };
    const theme = {
      assessment: draft,
      previous_assessment: previous,
      enabled: false,
    };
    const before = JSON.stringify(theme);
    assert.deepEqual(view.visibleAssessment(theme), {
      assessment: previous,
      previous: true,
    });
    assert.equal(JSON.stringify(theme), before);
    assert.deepEqual(
      view.visibleAssessment({ assessment: draft, previous_assessment: null }),
      { assessment: null, previous: false },
    );
  }
  const current = { id: "new", status: "ready" };
  assert.deepEqual(
    view.visibleAssessment({
      assessment: current,
      previous_assessment: previous,
    }),
    { assessment: current, previous: false },
  );
});

test("industry labels describe evidence direction and unknowns without inventing probabilities", () => {
  assert.equal(view.industryStateLabel("improving"), "改善迹象");
  assert.equal(view.industryStateLabel("mixed"), "信号分化");
  assert.equal(view.industryStateLabel("weakening"), "走弱迹象");
  assert.equal(view.industryStateLabel("insufficient_evidence"), "证据不足");
  assert.equal(view.industryStateLabel("future_new_state"), "状态待确认");
  assert.equal(view.assessmentStatusLabel("outcome_unknown"), "生成结果待确认");
  assert.equal(view.assessmentStatusLabel(), "尚无行业报告");
});

test("same-server credentials and demo never share an industry cache", () => {
  const first = { url: "https://radar.test", token: "first" };
  const cache = new Map([
    [JSON.stringify(view.industryQueryKey(first)), { privateReport: true }],
  ]);
  for (const second of [
    { ...first, token: "second" },
    { ...first, url: "https://other.test" },
    { ...first, demo: true },
  ])
    assert.equal(
      cache.get(JSON.stringify(view.industryQueryKey(second))),
      undefined,
    );
  assert.deepEqual(
    cache.get(JSON.stringify(view.industryQueryKey({ ...first }))),
    { privateReport: true },
  );
});

test("as-of age, calendar-only publication, and first-seen time remain distinct", () => {
  const now = Date.parse("2026-09-13T12:00:00Z");
  assert.match(
    view.assessmentFreshness("2026-09-10T12:00:00Z", now),
    /超过 48 小时/,
  );
  assert.match(
    view.assessmentFreshness("2026-09-13T11:00:00Z", now),
    /截至时间/,
  );
  assert.match(view.assessmentFreshness("not-a-date", now), /未知/);
  assert.match(
    view.assessmentFreshness("2026-09-14T12:00:00Z", now),
    /核对时钟/,
  );
  const evidence = {
    published_at: null,
    first_seen_at: "2026-09-13T12:00:00Z",
    published_precision: "unknown",
  };
  assert.equal(view.evidencePublishedLabel(evidence), "原始发布时间未提供");
  assert.equal(
    view.evidencePublishedLabel({
      ...evidence,
      published_at: "2026-09-12",
      published_precision: "date",
    }),
    "发布于 2026-09-12（仅日期，日内时间未知）",
  );
  assert.match(
    view.evidencePublishedLabel({
      ...evidence,
      published_at: "2026-09-12T12:00:00Z",
    }),
    /精度未确认/,
  );
  assert.equal(view.industryTime("invalid"), "时间未提供");
});

test("stale and failed sources are not presented as currently healthy", () => {
  const now = Date.parse("2026-09-13T12:00:00Z");
  const source = {
    status: "healthy",
    last_success_at: "2026-09-13T10:00:00Z",
    refresh_minutes: 60,
  };
  assert.match(view.industrySourceLabel(source, now), /超过计划间隔/);
  assert.equal(
    view.industrySourceLabel({ ...source, status: "error" }, now),
    "采集异常",
  );
  assert.equal(
    view.industrySourceLabel({ ...source, status: "disabled" }, now),
    "未启用",
  );
  assert.equal(
    view.industrySourceLabel({ ...source, last_success_at: null }, now),
    "成功时间未提供",
  );
  assert.equal(
    view.industrySourceLabel(
      { ...source, last_success_at: "2026-09-13T11:50:00Z" },
      now,
    ),
    "采集正常",
  );
});

test("a durable refresh acknowledgement is not proof of completed collection or analysis", () => {
  for (const status of ["queued", "running", "retrying"])
    assert.equal(view.industryJobActive({ status }), true);
  for (const status of [
    "completed",
    "failed",
    "needs_attention",
    "outcome_unknown",
    "unknown",
  ])
    assert.equal(view.industryJobActive({ status }), false);
  assert.equal(view.industryJobLabel({ status: "queued" }), "采集排队中");
  assert.equal(
    view.industryJobLabel({ status: "completed" }),
    "本轮采集已结束",
  );
  assert.equal(
    view.industryJobLabel({ status: "running" }, true),
    "上次状态：正在采集",
  );
  assert.equal(
    view.industryJobLabel({ status: "outcome_unknown" }),
    "采集结果待确认",
  );
});

test("source links accept web pages but reject native actions and embedded credentials", () => {
  assert.equal(
    view.originalWebURL("https://example.org/news?a=1#facts"),
    "https://example.org/news?a=1#facts",
  );
  for (const value of [
    "javascript:alert(1)",
    "file:///private/report",
    "intent://execute",
    "https://name:secret@example.org",
    "bad link",
  ])
    assert.equal(view.originalWebURL(value), null);
});

test("industry API uses bounded encoded evidence requests and reader-authorized mutations", async () => {
  const calls = [];
  class APIError extends Error {
    constructor(status, message) {
      super(message);
      this.status = status;
    }
  }
  const api = load("../src/industryApi.ts", {
    "./api": {
      APIError,
      api: async (...args) => {
        calls.push(args);
        return { id: "job-1", status: "queued", message: "待执行" };
      },
    },
  });
  const connection = { url: "https://radar.test", token: "reader" };
  const controller = new AbortController();
  await api.fetchIndustry(connection, controller.signal);
  assert.equal(calls.at(-1)[1], "/v1/industry");
  assert.equal(calls.at(-1)[2].signal, controller.signal);
  await api.fetchIndustryEvidence(connection, {
    theme: "ai/power",
    q: " price & power ",
    offset: 30,
    signal: controller.signal,
  });
  const url = new URL(calls.at(-1)[1], connection.url);
  assert.equal(url.searchParams.get("theme"), "ai/power");
  assert.equal(url.searchParams.get("q"), "price & power");
  assert.equal(url.searchParams.get("limit"), "30");
  assert.equal(url.searchParams.get("offset"), "30");
  await api.fetchIndustryEvidenceById(connection, "an/id?");
  assert.equal(calls.at(-1)[1], "/v1/industry/evidence/an%2Fid%3F");
  const job = await api.refreshIndustry(connection);
  assert.equal(job.status, "queued");
  assert.equal(calls.at(-1)[2].method, "POST");
  await api.fetchIndustryJob(connection, "job-1");
  assert.equal(calls.at(-1)[1], "/v1/industry/jobs/job-1");
  await api.setIndustryTracking(connection, "ai/power", false);
  assert.equal(calls.at(-1)[1], "/v1/industry/themes/ai%2Fpower/tracking");
  assert.equal(calls.at(-1)[2].method, "PUT");
  assert.deepEqual(JSON.parse(calls.at(-1)[2].body), { enabled: false });
  assert.equal(calls.at(-1)[0], connection);
  assert.equal(api.industryAccessDenied(new APIError(401, "expired")), true);
  assert.equal(api.industryAccessDenied(new APIError(403, "denied")), true);
  assert.match(
    api.industryError(new APIError(404, "missing")),
    /服务器尚未提供行业研究/,
  );
  assert.match(
    api.industryError(new APIError(404, "missing"), "evidence"),
    /无法核对引用/,
  );
  assert.match(
    api.industryError(new APIError(404, "missing"), "job"),
    /最新状态/,
  );
});
