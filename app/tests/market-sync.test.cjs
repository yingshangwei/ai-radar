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
      if (!(name in mocks)) throw new Error(`Unexpected ${name}`);
      return mocks[name];
    },
    module,
    module.exports,
  );
  return module.exports;
}
const sync = load("../src/marketSync.ts");
const { createMarketCache, mergeMarket, unpackMarket } = sync;
const revision = "a".repeat(64),
  nextRevision = "b".repeat(64);
const wire = () => ({
  schema: 1,
  generatedAt: 100,
  dashboard: {},
  waterlines: {},
  storage: {},
  charts: {
    buy: {
      source: "buy",
      range: { start: 0, end: 300 },
      order: ["10w"],
      series: {
        "10w": {
          name: "10w",
          layout: [["10:0", "20:0"], ["200:0"]],
          points: {
            "10:0": [10, 6, 6, 0],
            "20:0": [20, 7, 7, 0],
            "200:0": [200, 8, 8, 1],
          },
        },
      },
    },
  },
});
const snapshot = () => ({ revision, data: wire() });
function memory() {
  const values = new Map();
  return {
    values,
    async getItem(k) {
      return values.get(k) ?? null;
    },
    async setItem(k, v) {
      values.set(k, v);
    },
    async removeItem(k) {
      values.delete(k);
    },
  };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("versioned delta removes expired points, corrects old EMA, adds points and preserves gaps", () => {
  const base = snapshot();
  const patch = [
    1,
    {
      generatedAt: [0, 160],
      charts: [
        1,
        {
          buy: [
            1,
            {
              series: [
                1,
                {
                  "10w": [
                    1,
                    {
                      layout: [0, [["20:0", "30:0"], ["200:0"]]],
                      points: [
                        1,
                        {
                          "20:0": [0, [20, 7.1, 7, 0]],
                          "30:0": [0, [30, 7.2, 7.2, 0]],
                        },
                        ["10:0"],
                      ],
                    },
                    [],
                  ],
                },
                [],
              ],
            },
            [],
          ],
        },
        [],
      ],
    },
    [],
  ];
  const next = mergeMarket(base, {
    protocol: 2,
    kind: "delta",
    revision: nextRevision,
    baseRevision: revision,
    patch,
  });
  const strokes = unpackMarket(next).charts.buy.series[0].strokes;
  assert.deepEqual(
    strokes.map((s) => s.map((p) => p.time)),
    [[20, 30], [200]],
  );
  assert.equal(strokes[0][0].value, 7.1);
  assert.equal(unpackMarket(base).charts.buy.series[0].strokes[0][0].time, 10);
  assert.equal(
    mergeMarket(next, {
      protocol: 2,
      kind: "unchanged",
      revision: nextRevision,
      baseRevision: nextRevision,
    }),
    next,
  );
  assert.throws(() =>
    mergeMarket(base, {
      protocol: 2,
      kind: "delta",
      revision,
      baseRevision: nextRevision,
      patch,
    }),
  );
  assert.throws(() =>
    mergeMarket(base, {
      protocol: 2,
      kind: "delta",
      revision,
      baseRevision: revision,
      patch: [1, JSON.parse('{"__proto__":[0,{}]}'), []],
    }),
  );
});

test("disk cache survives process recreation; account/filter isolation, corruption and expiry", async () => {
  const io = memory(),
    cache = createMarketCache(io);
  await cache.save("account1", "bank", snapshot(), cache.generation());
  const restarted = createMarketCache(io);
  assert.deepEqual(await restarted.load("account1", "bank"), snapshot());
  assert.equal(await restarted.load("account2", "bank"), null);
  assert.equal(await restarted.load("account1", "alipay"), null);
  const key = [...io.values.keys()].find((k) => k.endsWith(".bank"));
  const record = JSON.parse(io.values.get(key));
  record.savedAt = 0;
  io.values.set(key, JSON.stringify(record));
  assert.equal(await restarted.load("account1", "bank"), null);
  io.values.set(key, "{broken");
  assert.equal(await restarted.load("account1", "bank"), null);
});

test("cache bounds and quota errors do not break updates; logout rejects late writes", async () => {
  const io = memory(),
    cache = createMarketCache(io),
    epoch = cache.generation();
  for (let i = 0; i < 8; i++)
    await cache.save("account", String(i), snapshot(), epoch);
  assert.equal(io.values.size, 5); // Four views and index.
  const clearing = cache.clear();
  await cache.save("account", "late", snapshot(), epoch);
  await clearing;
  assert.equal(io.values.size, 0);
  io.setItem = async () => {
    throw new Error("quota");
  };
  await createMarketCache(io).save("account", "bank", snapshot(), 0);
});

function harness({ cached = snapshot(), request, cache } = {}) {
  const states = [],
    refs = [];
  let si = 0,
    ri = 0,
    effect,
    cleanup,
    dependencies;
  const react = {
    useState(initial) {
      const i = si++;
      if (!(i in states)) states[i] = initial;
      return [
        states[i],
        (next) => {
          states[i] = typeof next === "function" ? next(states[i]) : next;
        },
      ];
    },
    useRef(initial) {
      const i = ri++;
      return (refs[i] ??= { current: initial });
    },
    useEffect(fn, deps) {
      if (!dependencies || deps.some((v, i) => v !== dependencies[i])) {
        cleanup?.();
        effect = fn;
        dependencies = deps;
      }
    },
  };
  class APIError extends Error {
    constructor(status) {
      super("expired");
      this.status = status;
    }
  }
  const store = cache ?? {
    generation: () => 0,
    load: async () => cached,
    save: async () => {},
    clear: async () => {},
  };
  const calls = [];
  const { useMarket } = load("../src/useMarket.ts", {
    react,
    "./api": {
      APIError,
      api: async (connection, path, options) => {
        calls.push({ connection, path, signal: options.signal });
        return request(APIError);
      },
    },
    "./marketPersistence": {
      marketCache: store,
      marketNamespace: async (c) => c.token,
    },
    "./marketSync": sync,
  });
  return {
    render(token = "reader", params = "payment=bank", active = true) {
      si = ri = 0;
      const value = useMarket({ url: "https://test", token }, params, active);
      if (effect) {
        const fn = effect;
        effect = null;
        cleanup = fn();
      }
      return value;
    },
    calls,
    close() {
      cleanup?.();
    },
  };
}

test("cold start renders persisted data before a slow network request and keeps it offline", async () => {
  let reject;
  const h = harness({
    request: () =>
      new Promise((_, no) => {
        reject = no;
      }),
  });
  h.render();
  await tick();
  assert.equal(h.render().data.generatedAt, 100);
  assert.equal(h.render().verified, false);
  assert.match(h.calls[0].path, /since=aaaa/);
  reject(new Error("offline"));
  await tick();
  assert.equal(h.render().data.generatedAt, 100);
  assert.equal(h.render().error.message, "offline");
  h.close();
});

test("auth rejection clears cached private data; inactive/obsolete requests cannot overwrite new filters", async () => {
  let clears = 0;
  const h = harness({
    request: (ErrorType) => Promise.reject(new ErrorType(401)),
    cache: {
      generation: () => 0,
      load: async () => snapshot(),
      save: async () => {},
      clear: async () => {
        clears++;
      },
    },
  });
  h.render();
  await tick();
  assert.equal(h.render().data, undefined);
  assert.equal(clears, 1);
  h.close();
  const waits = [];
  const other = harness({
    request: () => new Promise((resolve) => waits.push(resolve)),
  });
  other.render();
  await tick();
  const result = other.render("different-reader", "payment=alipay");
  assert.equal(result.data, undefined);
  assert.equal(other.calls[0].signal.aborted, true);
  await tick();
  const response = {
    protocol: 2,
    kind: "full",
    revision: nextRevision,
    data: { ...wire(), generatedAt: 999 },
  };
  waits[0](response);
  await tick();
  assert.equal(
    other.render("different-reader", "payment=alipay").data.generatedAt,
    100,
  );
  other.render("different-reader", "payment=alipay", false);
  assert.equal(other.calls[1].signal.aborted, true);
  waits[1](response);
  await tick();
  other.close();
});
