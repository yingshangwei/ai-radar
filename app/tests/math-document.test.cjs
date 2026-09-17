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
      esModuleInterop: true,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  new Function("exports", "require", source)(module.exports, (name) =>
    name.startsWith(".")
      ? load(path.resolve(path.dirname(file), name + ".ts"))
      : require(name),
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
  assert.ok(html.includes("&lt;img src="));
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

test("Markdown renders emphasis, code, tables and links while external content stays inert", () => {
  const html = math.mathDocument(
    '# 结论\n\n**重点**：见[来源](https://example.com/a)。\n\n- 第一项\n- 第二项\n\n| 模型 | 结果 |\n| --- | --- |\n| A | 1 |\n\n```sh\necho "$HOME"\n```\n\n![外部图片](https://tracker.invalid/a)\n\n[运行](javascript:alert(1))\n<script>alert(2)</script>',
    opts,
  );
  assert.match(html, /<strong>重点<\/strong>/);
  assert.match(html, /<h1>结论<\/h1>/);
  assert.match(html, /<table>/);
  assert.match(html, /<ul>/);
  assert.match(html, /<pre><code class="language-sh">/);
  assert.match(html, /href="https:\/\/example.com\/a"/);
  assert.doesNotMatch(html, /<img|href="javascript:|<script>alert/);
  assert.equal(
    math.richPreview("**重点** [来源](https://example.com)"),
    "重点 来源",
  );
  assert.match(math.richPlainText("```js\nconst x = 1;\n```"), /const x = 1/);
});
test("long prose has paragraph breaks without rewriting words; code and math stay unchanged", () => {
  const text = "这是一句完整的研究发现，保留事实与限定条件。".repeat(50);
  const formatted = math.readableParagraphs(text);
  assert.ok(formatted.includes("\n\n"));
  assert.equal(formatted.replace(/\n/g, ""), text);
  assert.equal(
    math.readableParagraphs("```\n" + text + "\n```"),
    "```\n" + text + "\n```",
  );
  const html = math.mathDocument(
    "**边界** $x_t^2$\n\n\\[x+y\\]\n\n`$code$`",
    opts,
  );
  assert.equal((html.match(/class="formula /g) || []).length, 2);
  assert.match(html, /<code>\$code\$<\/code>/);
});
