const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { dirname, resolve } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

const grants = (number) =>
  Object.fromEntries(
    Array.from({ length: number }, (_, index) => [
      `site-${String(index).padStart(3, "0")}.test`,
      { allowed: true, needsVerification: false },
    ]),
  );
const settle = async () => {
  for (let i = 0; i < 4; i++) await new Promise((done) => setImmediate(done));
};

function fixture() {
  const values = new Map(),
    cache = new Map(),
    listeners = new Set(),
    timers = new Map();
  const effects = [],
    requests = [];
  let sequence = 0,
    request = async () => [];
  const state = {
    currentState: "active",
    addEventListener: (_, callback) => {
      listeners.add(callback);
      return { remove: () => listeners.delete(callback) };
    },
  };
  const mocks = {
    react: {
      useEffect: (callback) => effects.push(callback),
      useRef: (current) => ({ current }),
      useState: (value) => [value, () => {}],
    },
    "react/jsx-runtime": {
      jsx: (type, props) => ({ type, props }),
      jsxs: (type, props) => ({ type, props }),
    },
    "react-native": {
      AppState: state,
      View: "View",
      useWindowDimensions: () => ({ width: 390, height: 844 }),
    },
    "@react-native-async-storage/async-storage": {
      default: {
        getItem: async (key) => values.get(key) || null,
        setItem: async (key, value) => values.set(key, value),
      },
    },
    "expo-secure-store": { getItemAsync: async () => null },
    "./DeviceArticleBrowser": { default: () => null },
    "./api": {
      api: async (connection, path) => {
        requests.push({ connection, path });
        return request();
      },
    },
  };
  function loadTS(path) {
    const filename = resolve(__dirname, path);
    if (cache.has(filename)) return cache.get(filename);
    const source = ts.transpileModule(readFileSync(filename, "utf8"), {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
        jsx: ts.JsxEmit.ReactJSX,
      },
    }).outputText;
    const module = { exports: {} };
    vm.runInThisContext(
      `(function(require,module,exports,setInterval,clearInterval){${source}\n})`,
      { filename },
    )(
      (name) => mocks[name] || loadTS(resolve(dirname(filename), name + ".ts")),
      module,
      module.exports,
      (callback, delay) => {
        const id = ++sequence;
        timers.set(id, { callback, delay });
        return id;
      },
      (id) => timers.delete(id),
    );
    cache.set(filename, module.exports);
    return module.exports;
  }
  const policy = loadTS("../src/deviceReadingPolicy.ts");
  const storage = loadTS("../src/deviceReadingStorage.ts");
  const component = loadTS("../src/DeviceReading.tsx").default;
  const server = "https://radar.test";
  return {
    ...policy,
    ...storage,
    server,
    requests,
    listeners,
    timers,
    setRequest: (fn) => (request = fn),
    permit: async (number) => {
      for (const domain of Object.keys(grants(number)))
        await storage.updateDeviceSite(server, domain, { allowed: true });
    },
    mount: (paused = false) => {
      component({
        connection: { url: server, token: "reader" },
        paused,
        onStatus: () => {},
        onComplete: () => {},
      });
      const cleanup = effects.splice(0).map((effect) => effect());
      return () => cleanup.forEach((stop) => stop?.());
    },
  };
}

test("bounded round-robin reaches every permitted domain instead of truncating the first thirty forever", () => {
  const { automaticDomains } = fixture();
  const permissions = grants(79),
    seen = new Set();
  let cursor = "";
  for (let round = 0; round < 3; round++) {
    const batch = automaticDomains(permissions, cursor);
    assert.equal(batch.length, 30);
    assert.equal(new Set(batch).size, batch.length);
    batch.forEach((domain) => seen.add(domain));
    cursor = batch.at(-1);
  }
  assert.equal(seen.size, 79);
});

test("revocation, verification pauses, additions and removed cursors preserve a valid next batch", () => {
  const { automaticDomains } = fixture();
  const permissions = grants(35);
  const first = automaticDomains(permissions);
  const cursor = first.at(-1);
  delete permissions[cursor];
  permissions["site-030.test"].allowed = false;
  permissions["site-031.test"].needsVerification = true;
  permissions["site-099.test"] = { allowed: true, needsVerification: false };
  const next = automaticDomains(permissions, cursor);
  assert.equal(next[0], "site-032.test");
  assert.ok(next.includes("site-099.test"));
  assert.ok(
    !next.includes(cursor) &&
      !next.includes("site-030.test") &&
      !next.includes("site-031.test"),
  );
  assert.ok(next.length <= 30);
  assert.deepEqual(automaticDomains({}, cursor), []);
  assert.deepEqual(
    automaticDomains({
      "www.example.test": { allowed: true },
      "example.test": { allowed: true },
    }),
    ["example.test"],
  );
});

test("actual DeviceReading persists rotation across short foreground sessions, without leaking timers", async () => {
  const f = fixture();
  await f.permit(31);
  for (let round = 0; round < 2; round++) {
    const stop = f.mount();
    await settle();
    assert.equal(f.timers.size, 1);
    assert.equal([...f.timers.values()][0].delay, 5 * 60 * 1000);
    stop();
    assert.equal(f.timers.size, 0);
    assert.equal(f.listeners.size, 0);
  }
  const batches = f.requests.map(({ path }) =>
    new URL(path, f.server).searchParams.get("domains").split(","),
  );
  assert.equal(batches.length, 2);
  assert.equal(batches[0].length, 30);
  assert.equal(batches[1][0], "site-030.test");
  assert.equal(new Set(batches.flat()).size, 31);
  assert.equal(await f.loadDeviceRotation(f.server), batches[1].at(-1));
  assert.equal(await f.loadDeviceRotation("https://other.test"), "");
  const stopPaused = f.mount(true);
  await settle();
  assert.equal(f.requests.length, 2);
  stopPaused();
});

test("failed or cancelled queue requests do not consume the persisted domain turn", async () => {
  const f = fixture();
  await f.permit(31);
  f.setRequest(async () => {
    throw new Error("offline");
  });
  let stop = f.mount();
  await settle();
  stop();
  assert.equal(await f.loadDeviceRotation(f.server), "");
  let finish;
  f.setRequest(() => new Promise((resolve) => (finish = resolve)));
  stop = f.mount();
  await settle();
  stop();
  finish([]);
  await settle();
  assert.equal(await f.loadDeviceRotation(f.server), "");
  assert.equal(f.timers.size, 0);
  assert.equal(f.listeners.size, 0);
});
