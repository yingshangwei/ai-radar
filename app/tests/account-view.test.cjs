const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const ts = require("typescript");
const source = ts.transpileModule(
  readFileSync(resolve(__dirname, "../src/accountView.ts"), "utf8"),
  {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  },
).outputText;
const mod = { exports: {} };
vm.runInThisContext(`(function(exports){${source}\n})`)(mod.exports);
const { money, windowName, accountBadge, orderedLimits } = mod.exports;

test("financial amounts distinguish unknown from zero or debt", () => {
  assert.equal(money(null, "CNY"), "—");
  assert.equal(money("", "CNY"), "—");
  assert.equal(money("0.00", "CNY"), "¥0.00");
  assert.equal(money("-2.50", "USD"), "$-2.50");
  assert.equal(money("NaN", "USD"), "—");
});
test("Codex main quota remains visible when the vendor lists reserve capacity first", () => {
  const limits = [
    { id: "base_model_inference" },
    { id: "codex" },
    { id: "codex_bengalfox" },
  ];
  assert.equal(orderedLimits(limits)[0].id, "codex");
  assert.equal(orderedLimits(limits).length, 3);
  assert.equal(limits[0].id, "base_model_inference");
});
test("actual quota windows, auth, expired snapshots and offline status stay distinct", () => {
  assert.equal(windowName(300), "5 小时额度");
  assert.equal(windowName(10080), "7 天额度");
  const base = { status: "ok", stale: false, checking: false, limits: [] };
  assert.equal(accountBadge(base), "已更新");
  assert.equal(accountBadge(base, true), "连接中断 · 历史数据");
  assert.equal(
    accountBadge({ ...base, status: "authorization_required", stale: true }),
    "待授权",
  );
  assert.equal(
    accountBadge({ ...base, status: "query_failed", stale: true }),
    "查询失败",
  );
  assert.equal(
    accountBadge({
      ...base,
      limits: [{ windows: [{ resets_at: "2020-01-01" }] }],
    }),
    "等待更新",
  );
});
