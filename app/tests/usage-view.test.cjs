const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const ts = require("typescript");
const source = ts.transpileModule(
  readFileSync(resolve(__dirname, "../src/usageView.ts"), "utf8"),
  {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  },
).outputText;
const mod = { exports: {} };
vm.runInThisContext(`(function(exports){${source}\n})`)(mod.exports);
const { tokenAmount, modelTotals } = mod.exports;

test("decimal token units promote through K M B, including rounding boundaries", () => {
  for (const [input, expected] of [
    [0, "0"],
    [999, "999"],
    [1000, "1K"],
    [12500, "12.5K"],
    [999999, "1M"],
    [1000000, "1M"],
    [12345678, "12.35M"],
    [999999999, "1B"],
    [1000000000, "1B"],
    [1234567890, "1.23B"],
    [null, "—"],
    [undefined, "—"],
    [-1, "—"],
    [NaN, "—"],
  ]) {
    assert.equal(tokenAmount(input), expected);
  }
});
test("model aggregation keeps providers distinct and missing usage unknown", () => {
  const rows = modelTotals([
    { provider: "codex", model: "m", calls: 1, total_tokens: null },
    { provider: "bailian", model: "m", calls: 2, total_tokens: 1000 },
    { provider: "bailian", model: "m", calls: 1, total_tokens: 500 },
  ]);
  assert.equal(rows[0].tokens, 1500);
  assert.equal(rows[0].calls, 3);
  assert.equal(rows[1].tokens, null);
});
