const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve, dirname } = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const ts = require("typescript");

function fixture() {
  const timers = new Map();
  let clock = 0;
  let sequence = 0;
  const effects = [];
  const listeners = new Set();
  const apiCalls = [];
  const injected = [];
  const failures = [];
  const saved = [];
  let grants = async () => ({ "example.test": { allowed: true } });
  const state = {
    currentState: "active",
    addEventListener: (_, fn) => {
      listeners.add(fn);
      return { remove: () => listeners.delete(fn) };
    },
  };
  const mocks = {
    react: {
      useRef: (current) => ({ current }),
      useState: (value) => [value, () => {}],
      useEffect: (fn) => effects.push(fn),
    },
    "react/jsx-runtime": {
      jsx: (type, props) => ({ type, props }),
      jsxs: (type, props) => ({ type, props }),
    },
    "react-native": { AppState: state, View: "View" },
    "react-native-webview": { WebView: "WebView" },
    "./api": {
      APIError: class APIError extends Error {},
      api: async (...args) => {
        apiCalls.push(args);
        return { ready: true, message: "saved" };
      },
    },
    "./deviceReadingStorage": {
      loadDeviceSites: () => grants(),
      updateDeviceSite: async () => {},
    },
  };
  const setTimer = (fn, delay) => {
    const id = ++sequence;
    timers.set(id, { due: clock + delay, fn });
    return id;
  };
  const clearTimer = (id) => timers.delete(id);
  function load(path) {
    const filename = resolve(__dirname, path);
    const source = ts.transpileModule(readFileSync(filename, "utf8"), {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
        target: ts.ScriptTarget.ES2022,
      },
    }).outputText;
    const target = { exports: {} };
    vm.runInThisContext(
      `(function(require,module,exports,setTimeout,clearTimeout) { ${source}\n})`,
      { filename },
    )(
      (name) => mocks[name] || load(resolve(dirname(filename), name + ".ts")),
      target,
      target.exports,
      setTimer,
      clearTimer,
    );
    return target.exports;
  }
  const component = load("../src/DeviceArticleBrowser.tsx").default;
  const url = "https://example.test/article";
  const element = component({
    connection: { url: "https://radar.test", token: "native-only" },
    document: { document_id: "a".repeat(64), domain: "example.test", url },
    visible: true,
    onSaved: (message) => saved.push(message),
    onFailure: (...args) => failures.push(args),
  });
  let reloads = 0;
  let stopped = 0;
  element.props.ref.current = {
    injectJavaScript: (script) => injected.push(script),
    reload: () => reloads++,
    stopLoading: () => stopped++,
  };
  const cleanup = effects.map((effect) => effect());
  const advance = (ms) => {
    const until = clock + ms;
    let limit = 1000;
    while (limit--) {
      const next = Array.from(timers.entries())
        .filter(([, timer]) => timer.due <= until)
        .sort((a, b) => a[1].due - b[1].due)[0];
      if (!next) break;
      timers.delete(next[0]);
      clock = next[1].due;
      next[1].fn();
    }
    clock = until;
  };
  const message = (patch = {}) => {
    const nonce = JSON.parse(
      injected.at(-1).match(/var requestNonce = ("[^"]+")/)[1],
    );
    element.props.onMessage({
      nativeEvent: {
        url,
        data: JSON.stringify({
          type: "radar.article",
          nonce,
          url,
          title: "Research report",
          text: "Verified article text, with enough context. ".repeat(20),
          links: [],
          partial: false,
          ...patch,
        }),
      },
    });
  };
  const activate = (value) => {
    state.currentState = value;
    listeners.forEach((fn) => fn(value));
  };
  const flush = async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  };
  return {
    load: () => element.props.onLoadEnd({ nativeEvent: { url } }),
    advance,
    message,
    activate,
    apiCalls,
    injected,
    saved,
    failures,
    flush,
    setGrants: (fn) => {
      grants = fn;
    },
    reloads: () => reloads,
    stopped: () => stopped,
    close: () => cleanup.forEach((fn) => fn?.()),
  };
}

test("visible browser stops extraction in background and never starts a new upload after a pending permission read", async () => {
  const f = fixture();
  f.load();
  f.advance(2000);
  assert.equal(f.injected.length, 1);
  f.activate("background");
  f.message();
  await f.flush();
  f.advance(30000);
  assert.equal(f.apiCalls.length, 0);
  assert.equal(f.injected.length, 1);
  assert.equal(f.stopped(), 1);
  f.activate("active");
  f.advance(2000);
  assert.equal(f.injected.length, 2);
  let resolveGrant;
  f.setGrants(
    () =>
      new Promise((resolve) => {
        resolveGrant = resolve;
      }),
  );
  f.message();
  f.activate("inactive");
  resolveGrant({ "example.test": { allowed: true } });
  await f.flush();
  assert.equal(f.apiCalls.length, 0);
  f.close();
});

test("visible verification keeps probing the same page past three checks, then uploads automatically without reload", async () => {
  const f = fixture();
  f.load();
  f.advance(2000);
  for (let count = 0; count < 5; count++) {
    f.message({ error: "verification_required" });
    f.advance(5000);
  }
  assert.equal(f.injected.length, 6);
  assert.equal(f.apiCalls.length, 0);
  f.message();
  await f.flush();
  assert.equal(f.apiCalls.length, 1);
  assert.equal(f.saved.length, 1);
  assert.equal(f.reloads(), 0);
  assert.ok(f.injected.every((script) => !script.includes("native-only")));
  assert.equal(JSON.parse(f.apiCalls[0][2].body).method, "mobile_browser");
  f.close();
});
