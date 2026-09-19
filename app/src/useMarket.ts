import { useEffect, useRef, useState } from "react";
import { api, APIError } from "./api";
import { marketCache, marketNamespace } from "./marketPersistence";
import {
  mergeMarket,
  unpackMarket,
  type MarketUpdate,
  type Snapshot,
} from "./marketSync";
import type { MarketView } from "./marketTypes";
import type { Connection } from "./types";

type State = {
  scope: string;
  data?: MarketView;
  error?: Error;
  fetching: boolean;
  verified: boolean;
  refreshing: boolean;
};
export function useMarket(
  connection: Connection,
  params: string,
  active: boolean,
) {
  const scope = JSON.stringify([connection.url, connection.token, params]);
  const [state, setState] = useState<State>({
    scope,
    fetching: false,
    verified: false,
    refreshing: false,
  });
  const refresh = useRef(() => {});
  useEffect(() => {
    if (!active || connection.url === "demo") return;
    const controller = new AbortController(),
      { signal } = controller;
    let timer: ReturnType<typeof setTimeout> | undefined,
      pending = false;
    let snapshot: Snapshot | null = null,
      namespace = "",
      generation = marketCache.generation();
    const updateState = (patch: Partial<State>) => {
      if (!signal.aborted)
        setState((old) => ({
          ...(old.scope === scope
            ? old
            : { scope, fetching: false, verified: false, refreshing: false }),
          ...patch,
        }));
    };
    const request = async () => {
      if (pending || signal.aborted) return;
      pending = true;
      clearTimeout(timer);
      updateState({ fetching: true });
      let delay = 60000;
      try {
        const fetchUpdate = (revision?: string) =>
          api<MarketUpdate>(
            connection,
            `/v1/market/view-update?${params}${revision ? `&since=${revision}` : ""}`,
            { signal },
            70000,
          );
        let response = await fetchUpdate(snapshot?.revision),
          next: Snapshot,
          data: MarketView | undefined;
        if (signal.aborted) return;
        try {
          next = mergeMarket(snapshot, response);
          data = next === snapshot ? undefined : unpackMarket(next);
        } catch {
          // Expired/corrupt local bases recover once, without a retry loop.
          response = await fetchUpdate();
          if (signal.aborted) return;
          next = mergeMarket(null, response);
          data = unpackMarket(next);
        }
        snapshot = next;
        updateState({
          ...(data ? { data } : {}),
          error: undefined,
          verified: true,
          refreshing: response.refreshing || response.ageSeconds > 90,
        });
        if (response.refreshing) delay = 15000;
        if (namespace)
          void marketCache.save(namespace, params, next, generation);
      } catch (error) {
        if (signal.aborted) return;
        const auth =
          error instanceof APIError && [401, 403].includes(error.status);
        updateState({
          error: error as Error,
          verified: false,
          ...(auth ? { data: undefined } : {}),
        });
        if (auth) {
          snapshot = null;
          await marketCache.clear();
          generation = marketCache.generation();
          delay = 0;
        }
      } finally {
        pending = false;
        updateState({ fetching: false });
        if (!signal.aborted && delay)
          timer = setTimeout(() => void request(), delay);
      }
    };
    void (async () => {
      updateState({ fetching: true, verified: false });
      try {
        namespace = await marketNamespace(connection);
        if (signal.aborted) return;
        generation = marketCache.generation();
        snapshot = await marketCache.load(namespace, params);
        if (signal.aborted) return;
        if (snapshot)
          updateState({
            data: unpackMarket(snapshot),
            fetching: false,
            error: undefined,
          });
      } catch {
        /* Secure/local storage failure still permits live reading. */
      }
      if (signal.aborted) return;
      refresh.current = () => void request();
      void request();
    })();
    return () => {
      controller.abort();
      clearTimeout(timer);
      refresh.current = () => {};
    };
  }, [scope, active]);
  const current =
    state.scope === scope
      ? state
      : { scope, fetching: active, verified: false, refreshing: false };
  return {
    ...current,
    isPending: !current.data && current.fetching,
    isRefetching: Boolean(current.data && current.fetching),
    refetch: () => refresh.current(),
  };
}
