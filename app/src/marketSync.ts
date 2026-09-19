import type { MarketView } from "./marketTypes";

export type Snapshot = { revision: string; data: any };
export type MarketUpdate = {
  protocol: 2;
  kind: "full" | "delta" | "unchanged";
  revision: string;
  baseRevision?: string;
  data?: any;
  patch?: any;
  refreshing: boolean;
  ageSeconds: number;
};
const safeKey = (key: string) =>
  !["__proto__", "prototype", "constructor"].includes(key);

/** Apply an immutable transport patch, preserving all stroke boundaries. */
function patch(base: any, change: any): any {
  if (!Array.isArray(change)) throw new Error("无效行情增量");
  if (change[0] === 0 && change.length === 2) return change[1];
  if (
    change[0] !== 1 ||
    !base ||
    Array.isArray(base) ||
    typeof base !== "object" ||
    !change[1] ||
    !Array.isArray(change[2])
  )
    throw new Error("行情版本不匹配");
  const next = { ...base };
  for (const key of change[2]) {
    if (!safeKey(key)) throw new Error("无效字段");
    delete next[key];
  }
  for (const [key, value] of Object.entries(change[1])) {
    if (!safeKey(key)) throw new Error("无效字段");
    next[key] = patch(base[key], value);
  }
  return next;
}

export function mergeMarket(
  base: Snapshot | null,
  update: MarketUpdate,
): Snapshot {
  if (update.protocol !== 2 || !/^[a-f0-9]{64}$/.test(update.revision))
    throw new Error("行情协议不匹配");
  if (update.kind === "full")
    return { revision: update.revision, data: update.data };
  if (!base || update.baseRevision !== base.revision)
    throw new Error("行情版本已失效");
  if (update.kind === "unchanged" && update.revision === base.revision)
    return base;
  if (update.kind !== "delta") throw new Error("无效行情响应");
  return { revision: update.revision, data: patch(base.data, update.patch) };
}

export function unpackMarket(snapshot: Snapshot): MarketView {
  const wire = snapshot.data;
  if (
    wire?.schema !== 1 ||
    !Number.isFinite(wire.generatedAt) ||
    !wire.dashboard ||
    !wire.waterlines ||
    !wire.charts ||
    !wire.storage
  )
    throw new Error("行情缓存无效");
  const charts = Object.fromEntries(
    Object.entries(wire.charts).map(([key, value]) => {
      const chart = value as any;
      const { order, series, ...meta } = chart;
      return [
        key,
        {
          ...meta,
          series: order.map((name: string) => {
            const { points, layout, ...details } = series[name];
            return {
              ...details,
              strokes: layout.map((ids: string[]) =>
                ids.map((id: string) => {
                  const p = points[id];
                  if (
                    !Array.isArray(p) ||
                    p.length !== 4 ||
                    !p.every(Number.isFinite)
                  )
                    throw new Error("行情数据点无效");
                  return { time: p[0], value: p[1], raw: p[2], run: p[3] };
                }),
              ),
            };
          }),
        },
      ];
    }),
  );
  return { ...wire, charts };
}

// Storage is injected so quota failures, cold starts and account isolation are testable.
export function createMarketCache(
  io: {
    getItem(key: string): Promise<string | null>;
    setItem(key: string, value: string): Promise<unknown>;
    removeItem(key: string): Promise<unknown>;
  },
  prefix = "airadar.cache.market.v2.",
) {
  let queue = Promise.resolve();
  let epoch = 0;
  const serial = <T>(work: () => Promise<T>): Promise<T> => {
    const result = queue.then(work);
    queue = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  };
  type Entry = { key: string; size: number; savedAt: number };
  const index = async (): Promise<Entry[]> => {
    try {
      const entries = JSON.parse((await io.getItem(prefix + "index")) ?? "[]");
      return Array.isArray(entries)
        ? entries.filter(
            (e) =>
              typeof e?.key === "string" &&
              e.key.startsWith(prefix) &&
              Number.isFinite(e.size) &&
              Number.isFinite(e.savedAt),
          )
        : [];
    } catch {
      return [];
    }
  };
  const key = (namespace: string, params: string) =>
    prefix + namespace + "." + encodeURIComponent(params);
  return {
    generation: () => epoch,
    load: (namespace: string, params: string) =>
      serial(async () => {
        try {
          const raw = await io.getItem(key(namespace, params));
          const record = raw && JSON.parse(raw);
          if (
            !record ||
            record.version !== 2 ||
            !Number.isFinite(record.savedAt) ||
            Date.now() - record.savedAt > 7 * 86400000 ||
            !/^[a-f0-9]{64}$/.test(record.snapshot?.revision)
          )
            return null;
          unpackMarket(record.snapshot);
          return record.snapshot as Snapshot;
        } catch {
          return null;
        }
      }),
    save: (
      namespace: string,
      params: string,
      snapshot: Snapshot,
      generation: number,
    ) =>
      serial(async () => {
        if (generation !== epoch) return;
        try {
          const id = key(namespace, params),
            savedAt = Date.now();
          const raw = JSON.stringify({ version: 2, savedAt, snapshot });
          // Below Android's per-row cursor limit; leave room for other app caches.
          if (raw.length * 2 > 1_500_000) return;
          const entries = (await index()).filter((e) => e.key !== id);
          entries.push({ key: id, size: raw.length * 2, savedAt });
          while (
            entries.length > 4 ||
            entries.reduce((n, e) => n + e.size, 0) > 3_000_000
          ) {
            await io.removeItem(entries.shift()!.key);
          }
          await io.setItem(prefix + "index", JSON.stringify(entries));
          await io.setItem(id, raw);
        } catch {
          /* A full disk must not discard a successful live update. */
        }
      }),
    clear: () => {
      epoch += 1; // Immediately invalidates writes started before logout/auth rejection.
      return serial(async () => {
        for (const entry of await index()) await io.removeItem(entry.key);
        await io.removeItem(prefix + "index");
      });
    },
  };
}
