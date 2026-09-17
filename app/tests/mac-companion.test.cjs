const test = require("node:test");
const assert = require("node:assert/strict");
test("Mac queue admits exact public targets and persists bounded backoff", async () => {
  const { identity, permitted, backoff, deferred } =
    await import("../mac/extension/policy.mjs");
  for (const url of [
    "file:///etc/passwd",
    "https://user:pass@example.com/a",
    "http://127.0.0.1/",
    "http://localhost/",
    "http://x.internal/a",
  ])
    assert.equal(identity(url), null);
  assert.equal(permitted("https://evil.example.com/a", ["example.com"]), false);
  assert.equal(permitted("https://www.example.com/a", ["example.com"]), true);
  assert.notEqual(
    identity("https://example.com/a"),
    identity("https://example.com/account"),
  );
  const retry = backoff(null, false, 1000);
  assert.deepEqual(deferred({ abc: retry }, 1000), ["abc"]);
  assert.deepEqual(deferred({ abc: retry }, retry.until + 1), []);
  assert.equal(backoff({ attempts: 50 }, false, 0).until, 6 * 3600000);
});

test("a challenged domain cannot crowd out the rest of the server queue", async () => {
  const { readyDomains } = await import("../mac/extension/policy.mjs");
  assert.deepEqual(
    readyDomains(
      ["openai.com", "anthropic.com"],
      [{ domain: "www.openai.com" }],
    ),
    ["anthropic.com"],
  );
});
