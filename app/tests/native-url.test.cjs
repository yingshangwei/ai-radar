const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { dirname, resolve } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { createRequire } = require("node:module");
const babel = require("@babel/core");
const ts = require("typescript");

// Exercise the exact URL shipped with this React Native version, not Node's
// WHATWG URL. Only the unrelated native Blob bridge is stubbed.
const blobRoot = dirname(require.resolve("react-native/Libraries/Blob/URL.js"));
const nativeCache = new Map();
function nativeModule(name) {
  const filename = resolve(blobRoot, name);
  if (nativeCache.has(filename)) return nativeCache.get(filename).exports;
  const module = { exports: {} };
  nativeCache.set(filename, module);
  const transformed = babel.transformSync(readFileSync(filename, "utf8"), {
    filename,
    configFile: false,
    babelrc: false,
    presets: [require.resolve("@react-native/babel-preset")],
  }).code;
  const nativeRequire = createRequire(filename);
  vm.runInThisContext(
    `(function(require, module, exports) { ${transformed}\n})`,
    {
      filename,
    },
  )(
    (dependency) => {
      if (dependency === "./NativeBlobModule")
        return { getConstants: () => ({}) };
      if (dependency === "./URLSearchParams")
        return nativeModule("URLSearchParams.js");
      return nativeRequire(dependency);
    },
    module,
    module.exports,
  );
  return module.exports;
}
const { URL: NativeURL, URLSearchParams: NativeSearchParams } =
  nativeModule("URL.js");
const context = vm.createContext({
  URL: NativeURL,
  URLSearchParams: NativeSearchParams,
});
const appCache = new Map();
function appModule(path) {
  const filename = resolve(__dirname, path);
  if (appCache.has(filename)) return appCache.get(filename).exports;
  const module = { exports: {} };
  appCache.set(filename, module);
  const source = ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  vm.runInContext(
    `(function(require,module,exports) { ${source}\n})`,
    context,
    {
      filename,
    },
  )(
    (name) => appModule(resolve(dirname(filename), name + ".ts")),
    module,
    module.exports,
  );
  return module.exports;
}
const { samePageURL, parseCaptureMessage } = appModule(
  "../src/mobileCapture.ts",
);
const { mobilePageURL, automaticCapture } = appModule(
  "../src/deviceReadingPolicy.ts",
);

test("actual React Native URL rejects setters; capture fragment matching works without them", () => {
  assert.equal(
    Object.getOwnPropertyDescriptor(NativeURL.prototype, "hash").set,
    undefined,
  );
  assert.equal(
    Object.getOwnPropertyDescriptor(NativeURL.prototype, "protocol").set,
    undefined,
  );
  assert.throws(
    () =>
      vm.runInContext(
        `'use strict'; const old = new URL('https://example.org/?article=1#section'); old.hash = '';`,
        context,
      ),
    /getter|read only|Cannot set/,
  );
  assert.equal(
    samePageURL(
      "https://example.org/?article=1",
      "https://example.org/?article=1#section",
    ),
    true,
  );
  assert.equal(
    samePageURL(
      "https://example.org/?article=1",
      "https://example.org/?article=2#section",
    ),
    false,
  );
  assert.equal(
    samePageURL(
      "https://example.org/?article=1",
      "https://other.org/?article=1",
    ),
    false,
  );
});

test("actual native URL supports HTTPS upgrade without a protocol setter", () => {
  assert.equal(
    mobilePageURL("http://example.org/?article=1#section"),
    "https://example.org/?article=1#section",
  );
  assert.equal(
    mobilePageURL("https://example.org/?article=1"),
    "https://example.org/?article=1",
  );
  assert.equal(mobilePageURL("file:///private/key"), "file:///private/key");
  assert.equal(
    mobilePageURL("https://name:secret@example.org/"),
    "https://name:secret@example.org/",
  );
});

test("native URL path from WebView reply to permitted auto import works for exact fixture queries", () => {
  const pending = {
    nonce: "fixture-native",
    url: "https://example.org/?radar_fixture=1",
  };
  const text =
    "This domain is for use in documentation examples without needing permission. Avoid use in operations.";
  const article = parseCaptureMessage(
    JSON.stringify({
      type: "radar.article",
      nonce: pending.nonce,
      url: pending.url,
      title: "Example Domain",
      text,
      links: [],
      partial: false,
    }),
    pending,
    pending.url + "#article",
  );
  assert.equal(article.text, text);
  const document = {
    document_id: "a".repeat(64),
    domain: "example.org",
    url: pending.url,
  };
  const grant = { "example.org": { allowed: true, needsVerification: false } };
  const payload = automaticCapture(document, article, grant);
  assert.equal(payload.document_id, document.document_id);
  assert.equal(payload.method, "mobile_browser");
  assert.equal(
    automaticCapture(
      document,
      { ...article, url: "https://example.org/?radar_fixture=2" },
      grant,
    ),
    null,
  );
  assert.equal(automaticCapture(document, article, {}), null);
});
