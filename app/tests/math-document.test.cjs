const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");
function load(file) {
  const module = { exports: {} };
  const source = ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  new Function("exports", "require", source)(module.exports, (name) =>
    load(path.resolve(path.dirname(file), name + ".ts")),
  );
  return module.exports;
}
const math = load(path.resolve(__dirname, "../src/mathDocument.ts"));
const opts = {
  nonce: "testnonce",
  fontSize: 16,
  lineHeight: 28,
  color: "#293326",
};
test("math delimiters preserve exact expressions, distinguish currency/code and handle multiline alignment", () => {
  const values = [
    "$x_t^2$",
    String.raw`\(\frac{1}{n}\)`,
    String.raw`\[x+y\]`,
    "$$a=b$$",
    String.raw`\begin{align}x&=y\\a&=b\end{align}`,
  ];
  const input = values.join(" 正文\n");
  assert.deepEqual(
    math.mathSpans(input).map((s) => s.source),
    values,
  );
  assert.equal(
    math.mathSpans("Price $5 and $10. `$x$` ```$$x$$``` $unfinished").length,
    0,
  );
  assert.equal(
    math.mathSpans(String.raw`Escaped \$5 plus $x$`)[0].source,
    "$x$",
  );
  assert.equal(math.mathPreview(input).match(/〔公式，详情查看〕/g).length, 5);
});
test("offline HTML escapes untrusted prose and restricts math/network capabilities", () => {
  const html = math.mathDocument(
    '<img src="https://invalid.test" onerror="alert(1)">$x_t$',
    opts,
  );
  assert.ok(html.includes("&lt;img src=&quot;"));
  assert.ok(!html.includes('<img src="https://invalid.test"'));
  assert.ok(html.includes("trust:false") && html.includes("maxExpand:300"));
  assert.ok(
    html.includes("default-src 'none'") && html.includes("font-src data:"),
  );
  assert.ok(!/url\((?!data:)/.test(html));
  assert.ok(html.includes("Permission is hereby granted"));
  assert.throws(() =>
    math.mathDocument("$x$", { ...opts, nonce: '"><script>' }),
  );
});
test("over-limit formulas and parse failures retain the complete source rather than clipping text", () => {
  const oversized = "$$" + "x".repeat(12001) + "$$";
  assert.ok(math.mathDocument(oversized, opts).includes(oversized));
  const many = Array.from({ length: 258 }, (_, i) => "$x_" + i + "$").join(
    "\n",
  );
  const html = math.mathDocument(many, opts);
  assert.equal((html.match(/class="formula /g) || []).length, 256);
  assert.ok(html.includes("$x_257$"));
  assert.ok(html.includes("el.textContent=el.dataset.source"));
});
