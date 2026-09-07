const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve, dirname } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

function loadTS(path) {
  const filename = resolve(__dirname, path);
  const source = ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const target = { exports: {} };
  vm.runInThisContext(`(function(require,module,exports) { ${source}\n})`, {
    filename,
  })(
    (name) => loadTS(resolve(dirname(filename), name + ".ts")),
    target,
    target.exports,
  );
  return target.exports;
}
const { isWebURL, parseCaptureMessage, extractionScript } = loadTS(
  "../src/mobileCapture.ts",
);
const { sitePresentation } = loadTS("../src/siteState.ts");
const {
  automaticCapture,
  automaticDomains,
  canRunDeviceReading,
  deferredDocumentIds,
  eligibleDeviceDocument,
  mobilePageURL,
  permittedPage,
  targetPage,
} = loadTS("../src/deviceReadingPolicy.ts");
const pending = {
  nonce: "one-user-click",
  url: "https://example.test/article",
};
const reply = (patch = {}) =>
  JSON.stringify({
    type: "radar.article",
    nonce: pending.nonce,
    url: pending.url,
    title: "An article",
    text: "A meaningful article paragraph. ".repeat(15),
    links: [],
    partial: false,
    ...patch,
  });

test("only ordinary HTTP(S) pages can navigate or supply article links", () => {
  for (const url of [
    "file:///private/key",
    "javascript:alert(1)",
    "intent://app",
    "data:text/html,abc",
    "https://user:pass@example.test/",
    "about:blank",
  ])
    assert.equal(isWebURL(url), false);
  assert.equal(isWebURL("https://example.test/article?x=1"), true);
  assert.equal(isWebURL("http://example.test/article"), true);
});

test("unsolicited, stale, oversized and wrong-page replies cannot produce a preview", () => {
  assert.equal(parseCaptureMessage(reply(), undefined, pending.url), null);
  assert.equal(
    parseCaptureMessage(
      reply({ nonce: "previous-click" }),
      pending,
      pending.url,
    ),
    null,
  );
  assert.equal(
    parseCaptureMessage(reply(), pending, "https://other.test/article"),
    null,
  );
  assert.equal(
    parseCaptureMessage("x".repeat(1500001), pending, pending.url),
    null,
  );
  assert.throws(() =>
    parseCaptureMessage(
      reply({ url: "https://other.test/article" }),
      pending,
      pending.url,
    ),
  );
  assert.throws(() =>
    parseCaptureMessage(
      reply({ text: "Checking your browser" }),
      pending,
      pending.url,
    ),
  );
  assert.throws(() =>
    parseCaptureMessage(
      reply({ links: [{ url: "file:///private/key", label: "secret" }] }),
      pending,
      pending.url,
    ),
  );
});

test("a matching single-click reply returns plain fields and ignores page-supplied secrets", () => {
  const article = parseCaptureMessage(
    reply({ token: "ignored", cookies: "ignored" }),
    pending,
    pending.url + "#section",
  );
  assert.equal(article.url, pending.url);
  assert.deepEqual(Object.keys(article).sort(), [
    "links",
    "partial",
    "text",
    "title",
    "url",
  ]);
});

test("collected public content never inherits legacy unverified authorization messaging", () => {
  const done = sitePresentation({
    pending: 0,
    status: "unverified",
    enabled: false,
    statuses: { fetched: 6 },
    message: "尚未验证",
  });
  assert.equal(done.action, "none");
  assert.equal(done.label, "正文已采集");
  assert.match(done.message, /无需登录或手动补采/);
  assert.equal(
    sitePresentation({
      pending: 3,
      status: "unverified",
      statuses: { pending: 3 },
    }).action,
    "automatic",
  );
  assert.equal(
    sitePresentation({ pending: 3, status: "rate_limited", statuses: {} })
      .action,
    "retry",
  );
  assert.equal(
    sitePresentation({ pending: 3, status: "auth_required", statuses: {} })
      .action,
    "login",
  );
});

test("one site permission permits automatic submission only for the queued article, never another host or path", () => {
  const document = {
    document_id: "a".repeat(64),
    domain: "example.test",
    url: pending.url,
  };
  const article = parseCaptureMessage(reply(), pending, pending.url);
  const allowed = {
    "example.test": { allowed: true, needsVerification: false },
  };
  assert.equal(automaticCapture(document, article, {}), null);
  assert.equal(
    automaticCapture(document, article, { "example.test": { allowed: false } }),
    null,
  );
  const payload = automaticCapture(document, article, allowed);
  assert.equal(payload.document_id, document.document_id);
  assert.equal(payload.method, "mobile_browser");
  assert.equal(
    automaticCapture(
      document,
      { ...article, url: "https://sub.example.test/article" },
      allowed,
    ),
    null,
  );
  assert.equal(
    automaticCapture(
      document,
      { ...article, url: "https://example.test/account" },
      allowed,
    ),
    null,
  );
  assert.equal(
    permittedPage("https://www.example.test/article", "example.test"),
    true,
  );
  assert.equal(
    permittedPage("https://evil.test/article", "example.test"),
    false,
  );
  assert.equal(
    targetPage(
      "https://example.test/article?utm_source=x",
      "http://www.example.test/article/",
    ),
    true,
  );
  assert.equal(
    mobilePageURL("http://example.test/article"),
    "https://example.test/article",
  );
});

test("automatic device work is foreground only; challenges pause a domain and network errors defer a document", () => {
  assert.equal(canRunDeviceReading("active", false, false), true);
  for (const state of ["background", "inactive", "unknown"])
    assert.equal(canRunDeviceReading(state, false, false), false);
  assert.equal(canRunDeviceReading("active", true, false), false);
  assert.equal(canRunDeviceReading("active", false, true), false);
  assert.deepEqual(
    deferredDocumentIds(
      { ["a".repeat(64)]: 2000, ["b".repeat(64)]: 500, invalid: 4000 },
      1000,
    ),
    ["a".repeat(64)],
  );
  const permissions = {
    "example.test": { allowed: true, needsVerification: false },
    "login.test": { allowed: true, needsVerification: true },
    "paused.test": { allowed: false, needsVerification: false },
  };
  assert.deepEqual(automaticDomains(permissions), ["example.test"]);
  const doc = {
    document_id: "a".repeat(64),
    domain: "example.test",
    url: pending.url,
  };
  assert.equal(eligibleDeviceDocument(doc, ["example.test"], {}, 1000), true);
  assert.equal(
    eligibleDeviceDocument(
      doc,
      ["example.test"],
      { [doc.document_id]: 1801000 },
      1000,
    ),
    false,
  );
  assert.equal(
    eligibleDeviceDocument(
      doc,
      ["example.test"],
      { [doc.document_id]: 1801000 },
      1801001,
    ),
    true,
  );
  assert.equal(
    eligibleDeviceDocument(
      { ...doc, url: "https://other.test/article" },
      ["example.test"],
      {},
      1000,
    ),
    false,
  );
  assert.throws(
    () =>
      parseCaptureMessage(
        reply({ error: "verification_required" }),
        pending,
        pending.url,
      ),
    (error) => error.reason === "verification",
  );
});

test(
  "Readability extracts article text/links without forms, hidden content, page scripts or network requests",
  {
    skip:
      !process.env.RADAR_TEST_CHROMIUM || !process.env.RADAR_TEST_PLAYWRIGHT,
  },
  async () => {
    const { chromium } = require(process.env.RADAR_TEST_PLAYWRIGHT);
    const browser = await chromium.launch({
      executablePath: process.env.RADAR_TEST_CHROMIUM,
    });
    try {
      const page = await browser.newPage();
      let requests = 0;
      const paragraph =
        "Researchers carefully evaluated an artificial intelligence model using independently collected examples. The measurements report both limitations and observed improvements, with exact numerical results and context. ";
      await page.route("**/*", async (route) => {
        requests++;
        await route.fulfill({
          contentType: "text/html",
          body: `<!doctype html><html><head><title>Model evaluation report</title><style>.private { display:none; }</style></head><body><header>Navigation to ignore</header><article><h1>Model evaluation report</h1>${Array.from({ length: 6 }, () => `<p>${paragraph.repeat(3)}</p>`).join("")}<p>Read the <a href="/paper">research paper</a> for details.</p><form style="display:none"><p>FORM_SECRET</p><input type="password" value="PASSWORD_SECRET"><textarea>TEXTAREA_SECRET</textarea></form><div contenteditable>EDITABLE_SECRET</div><p class="private">HIDDEN_SECRET</p><p aria-hidden="true">ARIA_SECRET</p><script>window.neverTransmit = 'SCRIPT_SECRET';</script></article></body></html>`,
        });
      });
      await page.goto(pending.url);
      await page.evaluate(() => {
        window.replies = [];
        window.ReactNativeWebView = {
          postMessage: (value) => window.replies.push(value),
        };
        Object.defineProperty(document, "cookie", {
          get() {
            throw Error("cookie must not be read");
          },
        });
      });
      assert.equal(await page.evaluate(() => window.replies.length), 0);
      await page.evaluate(extractionScript(pending.nonce));
      const raw = await page.evaluate(() => window.replies[0]);
      const article = parseCaptureMessage(raw, pending, pending.url);
      assert.match(article.text, /Researchers carefully evaluated/);
      for (const secret of [
        "FORM_SECRET",
        "PASSWORD_SECRET",
        "TEXTAREA_SECRET",
        "EDITABLE_SECRET",
        "HIDDEN_SECRET",
        "ARIA_SECRET",
        "SCRIPT_SECRET",
        "Navigation to ignore",
      ])
        assert.ok(!article.text.includes(secret), secret);
      assert.deepEqual(article.links, [
        { url: "https://example.test/paper", label: "research paper" },
      ]);
      assert.equal(
        requests,
        1,
        "extracting does not fetch links, scripts or a CDN",
      );
      assert.equal(
        await page.locator("input").inputValue(),
        "PASSWORD_SECRET",
        "cloned parsing preserves the visible page",
      );
      // A login overlay on the unchanged article URL is not readable yet.
      await page.evaluate(() => {
        const overlay = document.createElement("div");
        overlay.id = "login-overlay";
        overlay.innerHTML = '<input type="password">';
        document.body.appendChild(overlay);
      });
      await page.evaluate(extractionScript("overlay-click"));
      assert.equal(
        await page.evaluate(() => JSON.parse(window.replies.at(-1)).error),
        "verification_required",
      );
      await page.evaluate(() =>
        document.querySelector("#login-overlay").remove(),
      );
      // A visible Cloudflare frame also blocks capture without reading its contents.
      await page.evaluate(() => {
        const frame = document.createElement("iframe");
        frame.id = "challenge-fixture";
        frame.src = "https://challenges.cloudflare.com/turnstile/test";
        document.body.appendChild(frame);
      });
      await page.evaluate(extractionScript("challenge-click"));
      assert.equal(
        await page.evaluate(() => JSON.parse(window.replies.at(-1)).error),
        "verification_required",
      );
      await page.evaluate(() =>
        document.querySelector("#challenge-fixture").remove(),
      );
      // Completing the challenge only replaces DOM; no navigation or reload required.
      await page.evaluate(extractionScript("ready-after-verification"));
      assert.match(
        await page.evaluate(() => JSON.parse(window.replies.at(-1)).text),
        /Researchers carefully evaluated/,
      );
      await page.setContent(
        "<html><title>Just a moment</title><p>Verify you are human</p></html>",
      );
      await page.evaluate(extractionScript("next-click"));
      assert.equal(
        await page.evaluate(() => JSON.parse(window.replies.at(-1)).error),
        "verification_required",
      );
    } finally {
      await browser.close();
    }
  },
);
