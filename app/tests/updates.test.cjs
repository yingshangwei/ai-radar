const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const ts = require("typescript");
const source = ts.transpileModule(
  readFileSync(resolve(__dirname, "../src/updateState.ts"), "utf8"),
  {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  },
).outputText;
const mod = { exports: {} };
vm.runInThisContext(`(function(exports){${source}\n})`)(mod.exports);
const { updateCoordinator, updateMessage } = mod.exports;

test("foreground and manual checks coalesce, throttle, and recover after a network error", async () => {
  let time = 0,
    calls = 0,
    finish;
  const check = updateCoordinator(
    () => {
      calls++;
      return new Promise((resolve, reject) => {
        finish = { resolve, reject };
      });
    },
    () => time,
  );
  const first = check();
  assert.equal(check(true), first);
  await Promise.resolve();
  assert.equal(calls, 1);
  finish.reject(Error("offline"));
  await assert.rejects(first, /offline/);
  await check();
  assert.equal(calls, 1);
  time = 30_000;
  const retry = check(true);
  await Promise.resolve();
  finish.resolve();
  await retry;
  assert.equal(calls, 2);
  time += 15 * 60_000;
  const foreground = check();
  await Promise.resolve();
  finish.resolve();
  await foreground;
  assert.equal(calls, 3);
});

test("UI never claims current before a successful check and prioritizes a ready update", () => {
  const state = {
    enabled: true,
    pending: false,
    checking: false,
    downloading: false,
    failed: false,
    checked: false,
    emergency: false,
  };
  assert.match(updateMessage(state), /检查更新/);
  assert.match(
    updateMessage({ ...state, failed: true, checked: true }),
    /当前版本可继续使用/,
  );
  assert.match(
    updateMessage({ ...state, pending: true, failed: true }),
    /下次启动自动生效/,
  );
  assert.match(updateMessage({ ...state, emergency: true }), /内置版本/);
  assert.match(updateMessage({ ...state, checked: true }), /最新版本/);
});
