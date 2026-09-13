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

const { marketReading } = load("../src/marketView.ts");
const point = (time, value) => ({ time, value, raw: value, run: 0 });
const series = (strokes) => ({
  name: "waterline",
  strokes,
  minimum: 0,
  maximum: 10,
  lookupTolerance: 30,
});
test("market reading preserves gaps and does not present an old price as current", () => {
  const a = point(100, 6.6),
    b = point(160, 6.7),
    c = point(1000, 6.8);
  const s = series([[a, b], [c]]);
  assert.equal(marketReading(s, 600), null);
  assert.equal(marketReading(s, 160), b);
  assert.equal(marketReading(s, 1040), null);
  assert.equal(marketReading(s, 990), c);
});
test("decimated chart reading reports the original sample timestamp, including single points", () => {
  const a = point(0, 6),
    b = point(600, 7);
  assert.equal(marketReading(series([[a, b]]), 400), b);
  assert.equal(marketReading(series([[b]]), 600).time, 600);
  assert.equal(marketReading(series([]), 600), null);
});
