const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { dirname, resolve } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const {
  QueryClient,
  QueryObserver,
  focusManager,
} = require("@tanstack/react-query");

function loadTS(path, mocks = {}) {
  const filename = resolve(__dirname, path);
  const source = ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const module = { exports: {} };
  vm.runInThisContext(`(function(require,module,exports,fetch){${source}\n})`, {
    filename,
  })(
    (name) =>
      mocks[name] || loadTS(resolve(dirname(filename), name + ".ts"), mocks),
    module,
    module.exports,
    mocks.fetch || fetch,
  );
  return module.exports;
}
const {
  contentRevision,
  contentRefreshTracker,
  subscribeNativeFocus,
  syncArticleBookmark,
} = loadTS("../src/contentSync.ts");
const { sourceStatusLabel, sourceConnected } = loadTS("../src/sourceState.ts");
const status = () => ({
  article_count: 46,
  translation: {
    counts: { ready: 46 },
    resource_counts: { ready: 13, review_required: 34 },
  },
  sources: [{ id: "x", last_success_at: "2026-09-08T01:00:00Z" }],
  jobs: [
    { id: "daily", status: "running", started_at: "now", message: "working" },
  ],
});
const snapshot = (data) => ({ data, offline: false });
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("status revisions ignore polling metadata and order but track published content changes", () => {
  const initial = status();
  const equivalent = structuredClone(initial);
  equivalent.sources[0].message = "new status text";
  equivalent.sources[0].last_attempt_at = "later";
  equivalent.translation.resource_counts = { review_required: 34, ready: 13 };
  equivalent.jobs[0].message = "still running";
  equivalent.server_now = "2026-09-08T02:00:00Z";
  equivalent.jobs[0].heartbeat_at = "2026-09-08T02:00:00Z";
  equivalent.jobs[0].progress_at = "2026-09-08T01:30:00Z";
  equivalent.jobs[0].phase = "translate";
  equivalent.jobs[0].attempt = 2;
  equivalent.jobs[0].retry_at = "2026-09-08T03:00:00Z";
  equivalent.jobs.unshift({ id: "read", status: "running" });
  assert.equal(contentRevision(initial), contentRevision(equivalent));
  for (const change of [
    (value) => value.article_count++,
    (value) => value.translation.counts.ready++,
    (value) => value.translation.resource_counts.ready++,
    (value) => (value.sources[0].last_success_at = "later"),
    (value) => (value.jobs[0].status = "completed"),
    (value) => (value.jobs[0].status = "failed"),
    (value) => (value.jobs[0].status = "needs_attention"),
  ]) {
    const changed = structuredClone(initial);
    change(changed);
    assert.notEqual(contentRevision(initial), contentRevision(changed));
  }
});

test("real QueryClient refreshes active content once, leaves status and other servers alone", async () => {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
    },
  });
  const calls = new Map();
  const unsubscribe = [];
  const keys = [
    "articles",
    "article",
    "digest",
    "editions",
    "status",
    "watches",
  ].map((kind) => ["server", "live", kind]);
  keys.push(["other", "live", "articles"], ["server", "demo", "digest"]);
  for (const queryKey of keys) {
    const id = JSON.stringify(queryKey);
    client.setQueryData(queryKey, { old: true });
    const observer = new QueryObserver(client, {
      queryKey,
      queryFn: async () => {
        calls.set(id, (calls.get(id) || 0) + 1);
        return { fresh: true };
      },
    });
    unsubscribe.push(observer.subscribe(() => {}));
  }
  const inactive = ["server", "live", "article", "closed-detail"];
  client.setQueryData(inactive, { old: true });
  const observe = contentRefreshTracker(client, ["server", "live"]);
  try {
    const initial = status();
    assert.equal(await observe(snapshot(initial)), false);
    for (let poll = 0; poll < 10; poll++)
      assert.equal(await observe(snapshot(structuredClone(initial))), false);
    assert.equal(calls.size, 0);
    const finished = structuredClone(initial);
    finished.jobs[0].status = "completed";
    finished.jobs[0].finished_at = "finished";
    assert.equal(await observe(snapshot(finished)), true);
    assert.equal(client.getQueryState(inactive).isInvalidated, true);
    for (const queryKey of keys) {
      assert.equal(
        calls.get(JSON.stringify(queryKey)) || 0,
        queryKey[0] === "server" &&
          queryKey[1] === "live" &&
          ["articles", "article", "digest", "editions"].includes(queryKey[2])
          ? 1
          : 0,
      );
    }
    assert.equal(await observe({ data: initial, offline: true }), false);
    assert.equal(await observe(snapshot(finished)), true);
    assert.equal(await observe(snapshot(finished)), false);
    const translated = structuredClone(finished);
    translated.translation.resource_counts = { ready: 14, review_required: 33 };
    assert.equal(await observe({ data: translated, offline: true }), false);
    assert.equal(await observe(snapshot(translated)), true);
    for (let poll = 0; poll < 10; poll++)
      assert.equal(await observe(snapshot(translated)), false);
    assert.equal(calls.get(JSON.stringify(["server", "live", "articles"])), 3);
    assert.equal(
      calls.get(JSON.stringify(["server", "live", "status"])) || 0,
      0,
    );
  } finally {
    unsubscribe.forEach((stop) => stop());
    client.clear();
  }
});

test("cold offline startup refreshes cached content once on recovery even with identical status", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, gcTime: Infinity } },
  });
  const queryKey = ["server", "live", "articles"];
  const statusKey = ["server", "live", "status"];
  const oldStatus = status();
  client.setQueryData(queryKey, { offline: true, data: "old" });
  client.setQueryData(statusKey, snapshot(oldStatus));
  let calls = 0;
  const observer = new QueryObserver(client, {
    queryKey,
    queryFn: async () => {
      calls++;
      return { offline: false, data: "fresh" };
    },
  });
  const unsubscribe = observer.subscribe(() => {});
  const observe = contentRefreshTracker(client, ["server", "live"]);
  try {
    assert.equal(await observe(), false);
    for (let poll = 0; poll < 5; poll++)
      assert.equal(await observe({ data: oldStatus, offline: true }), false);
    assert.equal(calls, 0);
    assert.equal(await observe(snapshot(oldStatus)), true);
    assert.equal(client.getQueryData(queryKey).data, "fresh");
    assert.equal(client.getQueryState(statusKey).isInvalidated, false);
    for (let poll = 0; poll < 10; poll++)
      assert.equal(await observe(snapshot(oldStatus)), false);
    assert.equal(calls, 1);
  } finally {
    unsubscribe();
    client.clear();
  }
});

test("acknowledged bookmark survives a cancelled old read and offline detail refetch", async () => {
  const values = new Map();
  let slowRead = true,
    readSignal,
    putSaved;
  const { api, cached, saveCached } = loadTS("../src/api.ts", {
    "react-native": { Platform: { OS: "android" } },
    "expo-secure-store": {},
    "@react-native-async-storage/async-storage": {
      default: {
        getItem: async (key) => values.get(key) || null,
        setItem: async (key, value) => values.set(key, value),
      },
    },
    fetch: async (url, options) => {
      if (options.method === "PUT") {
        putSaved = JSON.parse(options.body).saved;
        return { ok: true, json: async () => ({ saved: putSaved }) };
      }
      if (slowRead) {
        readSignal = options.signal;
        return new Promise((resolve, reject) => {
          options.signal.addEventListener("abort", () => {
            // Even a transport that resolves after cancellation cannot save stale content.
            resolve({
              ok: true,
              json: async () => ({ ...article, saved: false }),
            });
          });
        });
      }
      throw new Error("offline fixture");
    },
  });
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
    },
  });
  const connection = { url: "https://radar.test", token: "test-only" };
  const prefix = [connection.url, "live"];
  const article = {
    id: "article-1",
    saved: false,
    text: "fresh detail",
    resources: ["retained"],
  };
  const queryKey = [...prefix, "article", article.id];
  const otherKey = ["https://other.test", "live", "article", article.id];
  const path = `/v1/articles/${article.id}`;
  await saveCached(connection, path, article);
  client.setQueryData(queryKey, { data: article, offline: false });
  client.setQueryData(otherKey, { data: article, offline: false });
  const observer = new QueryObserver(client, {
    queryKey,
    queryFn: ({ signal }) => cached(connection, path, signal),
  });
  const unsubscribe = observer.subscribe(() => {});
  try {
    const pendingRead = observer.refetch();
    await settle();
    const result = await api(connection, `${path}/bookmark`, {
      method: "PUT",
      body: JSON.stringify({ saved: true }),
    });
    const updated = await syncArticleBookmark(
      client,
      prefix,
      { ...article, text: "old card" },
      result.saved,
    );
    assert.equal(readSignal.aborted, true);
    await saveCached(connection, path, updated);
    await pendingRead;
    assert.equal(updated.saved, true);
    assert.equal(updated.text, "fresh detail");
    assert.deepEqual(updated.resources, ["retained"]);
    assert.equal(client.getQueryData(otherKey).data.saved, false);
    assert.equal(
      JSON.parse(values.get(`airadar.cache.${connection.url}${path}`)).saved,
      true,
    );
    slowRead = false;
    await client.invalidateQueries({ queryKey, exact: true });
    assert.equal(client.getQueryData(queryKey).offline, true);
    const shown = client.getQueryData(queryKey).data;
    assert.equal(shown.saved, true);
    const second = await api(connection, `${path}/bookmark`, {
      method: "PUT",
      body: JSON.stringify({ saved: !shown.saved }),
    });
    assert.equal(putSaved, false);
    await syncArticleBookmark(client, prefix, shown, second.saved);
    assert.equal(client.getQueryData(queryKey).data.saved, false);
  } finally {
    unsubscribe();
    client.clear();
  }
});

test("React Native AppState resumes stale queries through official focusManager, with cleanup", async () => {
  const listeners = new Set();
  const appState = {
    currentState: "background",
    addEventListener: (event, listener) => {
      assert.equal(event, "change");
      listeners.add(listener);
      return { remove: () => listeners.delete(listener) };
    },
  };
  const stopFocus = subscribeNativeFocus(appState, "android", focusManager);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  client.mount();
  let calls = 0;
  const observer = new QueryObserver(client, {
    queryKey: ["fresh-on-return"],
    initialData: "old",
    staleTime: 0,
    refetchOnMount: false,
    queryFn: async () => {
      calls++;
      return "new";
    },
  });
  const unsubscribe = observer.subscribe(() => {});
  try {
    assert.equal(calls, 0);
    assert.equal(listeners.size, 1);
    listeners.forEach((listener) => listener("active"));
    await settle();
    assert.equal(calls, 1);
    assert.equal(observer.getCurrentResult().data, "new");
    listeners.forEach((listener) => listener("active"));
    await settle();
    assert.equal(calls, 1);
  } finally {
    unsubscribe();
    client.unmount();
    client.clear();
    stopFocus();
  }
  assert.equal(listeners.size, 0);
  const webStop = subscribeNativeFocus(appState, "web", {
    setFocused: () => assert.fail("web focus overridden"),
  });
  assert.equal(listeners.size, 0);
  webStop();
});

test("partial is connected but clearly marked as incomplete coverage", () => {
  assert.equal(sourceStatusLabel("partial"), "部分覆盖");
  assert.equal(sourceConnected("partial"), true);
  assert.equal(sourceConnected("healthy"), true);
  assert.equal(sourceConnected("auth_required"), false);
  assert.equal(sourceStatusLabel("auth_required"), "待授权");
});
