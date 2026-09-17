const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const query = require("@tanstack/react-query");

class APIError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}
const requests = [];
const primitive =
  (tag) =>
  ({ children, accessibilityLabel, disabled }) =>
    React.createElement(
      tag,
      {
        "aria-label": accessibilityLabel,
        ...(tag === "button" ? { disabled } : {}),
      },
      children,
    );
const native = {
  View: primitive("div"),
  Text: primitive("span"),
  Pressable: primitive("button"),
  ScrollView: primitive("section"),
  ActivityIndicator: primitive("progress"),
  TextInput: primitive("input"),
  Modal: primitive("aside"),
  RefreshControl: () => null,
  StyleSheet: { create: (value) => value },
  Linking: { openURL: async () => {} },
};
const modules = new Map();
const mocks = {
  "./MathText": { default: ({ text }) => React.createElement("p", null, text) },
  react: React,
  "react/jsx-runtime": require("react/jsx-runtime"),
  "react-native": native,
  "react-native-safe-area-context": { SafeAreaView: native.View },
  "@tanstack/react-query": query,
  "./api": {
    APIError,
    api: async (...args) => {
      requests.push(args);
      throw new Error("Unexpected request in paused render");
    },
  },
};
function load(name) {
  if (mocks[name]) return mocks[name];
  if (modules.has(name)) return modules.get(name);
  const extension = name === "./IndustryPanel" ? ".tsx" : ".ts";
  const filename = resolve(__dirname, "../src", name + extension);
  const source = ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
  const module = { exports: {} };
  vm.runInThisContext(`(function(require,module,exports){${source}\n})`, {
    filename,
  })(load, module, module.exports);
  modules.set(name, module.exports);
  return module.exports;
}
const Panel = load("./IndustryPanel").default;
const { industryQueryKey } = load("./industryView");
const connection = {
  url: "https://industry-fixture.invalid",
  token: "fixture-reader",
};
const assessment = {
  id: "fixture-report",
  status: "ready",
  as_of: "2026-09-13T08:00:00Z",
  created_at: "2026-09-13T08:02:00Z",
  state: "insufficient_evidence",
  summary_zh: "已审核的历史判断：尚不能证明经营改善。",
  supporting: [
    { text_zh: "仅有一个主体披露初步进展。", source_ids: ["fixture-evidence"] },
  ],
  opposing: [],
  investment_implications: [],
  watch_items: [],
  unknowns: ["市场预期差未知。"],
  horizon: "两个季度",
  evidence_ids: ["fixture-evidence"],
};
const theme = {
  id: "fixture-theme",
  name: "测试行业",
  description: "测试主题描述",
  enabled: true,
  hypothesis: "测试假说",
  indicators: ["测试经营指标"],
  risks: ["测试反证"],
  companies: [],
  evidence_count: 1,
  independent_sources: 1,
  latest_evidence_at: "2026-09-13T08:00:00Z",
  state: "insufficient_evidence",
  assessment,
  previous_assessment: null,
  analysis_status: { status: "ready" },
};
const report = {
  enabled: true,
  free_only: true,
  as_of: "2026-09-13T09:00:00Z",
  scope: "测试覆盖范围",
  limitations: ["测试覆盖限制"],
  sources: [],
  themes: [theme],
  latest_job: null,
};

function render({
  data = report,
  error,
  targetConnection = connection,
  seededConnection = connection,
} = {}) {
  const client = new query.QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  const key = [...industryQueryKey(seededConnection), "report"];
  if (data) client.setQueryData(key, data);
  if (error)
    client
      .getQueryCache()
      .find({ queryKey: key, exact: true })
      .setState({ error, status: "error" });
  const html = renderToStaticMarkup(
    React.createElement(
      query.QueryClientProvider,
      { client },
      React.createElement(Panel, {
        connection: targetConnection,
        active: false,
      }),
    ),
  );
  client.clear();
  return html;
}

test("industry panel keeps a ready report visible while a new analysis is running", () => {
  const html = render({
    data: {
      ...report,
      themes: [{ ...theme, analysis_status: { status: "calling" } }],
    },
  });
  assert.match(html, /正在生成或审核报告/);
  assert.match(html, /上次已完成报告/);
  assert.match(html, /已审核的历史判断/);
  assert.match(html, /报告截至/);
  assert.match(html, /原文 · fixture-/);
  assert.match(html, /不是股票涨跌概率/);
});

test("paused tracking retains history, while unapproved assessment text stays hidden", () => {
  const history = render({
    data: { ...report, themes: [{ ...theme, enabled: false }] },
  });
  assert.match(history, /已暂停跟踪/);
  assert.match(history, /已审核的历史判断/);
  const pending = render({
    data: {
      ...report,
      themes: [
        {
          ...theme,
          assessment: {
            ...assessment,
            status: "needs_attention",
            summary_zh: "未审核秘密草稿",
          },
          analysis_status: { status: "needs_attention" },
        },
      ],
    },
  });
  assert.match(pending, /报告待处理/);
  assert.doesNotMatch(pending, /未审核秘密草稿/);
});

test("old servers and rejected credentials cannot render previous cached industry reports", () => {
  const old = render({ error: new APIError(404, "missing") });
  assert.match(old, /服务器尚未提供行业研究/);
  assert.doesNotMatch(old, /已审核的历史判断/);
  for (const status of [401, 403]) {
    const denied = render({ error: new APIError(status, "denied") });
    assert.match(denied, /访问未通过验证/);
    assert.doesNotMatch(denied, /已审核的历史判断/);
  }
  const offline = render({ error: new Error("网络中断") });
  assert.match(offline, /当前显示上次取得的数据/);
  assert.match(offline, /已审核的历史判断/);
});

test("demo, fresh credentials, loading, and an empty server do not invent real industry data", () => {
  assert.match(
    render({ targetConnection: { url: "", token: "", demo: true } }),
    /示例模式没有真实行业报告/,
  );
  const switched = render({
    targetConnection: { ...connection, token: "different-reader" },
  });
  assert.match(switched, /正在读取行业研究/);
  assert.doesNotMatch(switched, /已审核的历史判断/);
  assert.match(render({ data: { ...report, themes: [] } }), /尚未配置行业主题/);
  assert.equal(requests.length, 0);
});

test("queued collection remains queued even when its submission succeeded", () => {
  const html = render({
    data: {
      ...report,
      latest_job: {
        id: "fixture-job",
        status: "queued",
        message: "等待采集通道",
      },
    },
  });
  assert.match(html, /采集排队中/);
  assert.match(html, /等待采集通道/);
  assert.doesNotMatch(html, /本轮采集已结束/);
});

test("industry source costs and processing-stage limits are not mistaken for free model calls", () => {
  const html = render({
    data: {
      ...report,
      budget: {
        calls_today: 2,
        max_calls_per_day: 12,
        analysis_enabled: false,
        unit: "model_stage",
        provider_calls_per_stage_max: 2,
      },
    },
  });
  assert.match(html, /自动分析未启用/);
  assert.match(html, /模型处理阶段 2\/12/);
  assert.match(html, /数据来源免费，模型沿用现有额度/);
  assert.match(html, /不是实际模型请求上限/);
});
