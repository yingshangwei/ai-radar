const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const ts = require("typescript");
const source = ts.transpileModule(
  readFileSync(resolve(__dirname, "../src/articlePresentation.ts"), "utf8"),
  {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  },
).outputText;
const mod = { exports: {} };
vm.runInThisContext(`(function(exports){${source}\n})`)(mod.exports);
const { articleContent, displayHeadline, publicationLabel, authorInitials } =
  mod.exports;
const header = "[引用帖：@example，2026-09-06T00:00:00Z]";
const fixture = () => ({
  platform: "x",
  title: "Original title",
  text: `My own statement.\n\n${header}\nTheir original statement.`,
  translation: { status: "ready" },
  title_zh: "原始标题的翻译",
  text_zh: `我自己的发言。\n\n${header}\n对方的原始发言。`,
});

test("only a server-approved title is labelled AI", () => {
  const a = fixture();
  a.resources = [{ relation: "source", status: "ready", title_zh: "关联标题" }];
  assert.deepEqual(displayHeadline(a), { text: a.title_zh, ai: false });
  a.presentation = { status: "review_required", title_zh: "待审候选" };
  assert.equal(displayHeadline(a).ai, false);
  a.presentation = { status: "ready", title_zh: "已保存的AI标题" };
  assert.deepEqual(displayHeadline(a), { text: "已保存的AI标题", ai: true });
});
test("both language modes preserve separate authors and full text without mutation", () => {
  const a = fixture(),
    before = JSON.stringify(a);
  const zh = articleContent(a),
    en = articleContent(a, true);
  assert.equal(zh.body, "我自己的发言。");
  assert.equal(en.body, "My own statement.");
  assert.equal(zh.quotes[0].identity, "@example");
  assert.equal(zh.quotes[0].publishedAt, en.quotes[0].publishedAt);
  assert.equal(zh.quotes[0].text, "对方的原始发言。");
  assert.equal(en.quotes[0].text, "Their original statement.");
  assert.equal(JSON.stringify(a), before);
});
test("missing or changed translated markers do not invent attribution", () => {
  for (const text of [
    "我自己的发言。对方的发言。",
    fixture().text_zh.replace("@example", "@different"),
  ]) {
    const a = fixture();
    a.text_zh = text;
    assert.deepEqual(articleContent(a), { body: text, quotes: [] });
  }
});
test("unapproved Chinese candidates are hidden", () => {
  const a = fixture();
  a.translation.status = "review_required";
  assert.equal(articleContent(a).body, "My own statement.");
  assert.equal(articleContent(a).quotes[0].text, "Their original statement.");
});
test("mentions and malformed markers are not inferred as relationships", () => {
  const a = fixture();
  a.translation.status = "pending";
  for (const text of [
    "Thanks @example.",
    "[引用帖：@example，not-a-date]\nWords",
    `Text ${header} inline.`,
  ]) {
    a.text = text;
    assert.deepEqual(articleContent(a), { body: text, quotes: [] });
  }
});
test("multiple and unavailable quotes retain all available detail bodies", () => {
  const a = fixture();
  a.translation.status = "pending";
  a.text +=
    "\n\n[引用帖不可用，未取得原文]\n\n[引用帖：作者未返回，发布时间未返回]\nAnother body.";
  const result = articleContent(a);
  assert.equal(result.quotes.length, 3);
  assert.equal(result.quotes[0].text, "Their original statement.");
  assert.equal(result.quotes[1].unavailable, true);
  assert.equal(result.quotes[2].text, "Another body.");
});
test("date precision and initials remain meaningful", () => {
  assert.equal(publicationLabel("not-a-date"), "时间未提供");
  assert.doesNotMatch(
    publicationLabel("2026-09-06T00:00:00Z", true),
    /\d{2}:\d{2}/,
  );
  assert.equal(authorInitials("Research Team"), "RT");
  assert.equal(authorInitials("林遥"), "林");
});
