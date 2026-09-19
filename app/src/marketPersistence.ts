import AsyncStorage from "@react-native-async-storage/async-storage";
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";
import type { Connection } from "./types";
import { createMarketCache } from "./marketSync";

export const marketCache = createMarketCache(AsyncStorage);
const key = "airadar.market.identity.v2";
let queue = Promise.resolve();
function serial<T>(fn: () => Promise<T>): Promise<T> {
  const result = queue.then(fn);
  queue = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}
export function marketNamespace(connection: Connection): Promise<string> {
  return serial(async () => {
    const raw =
      Platform.OS === "web"
        ? sessionStorage.getItem(key)
        : await SecureStore.getItemAsync(key);
    let saved: { url: string; token: string; namespace: string } | null = null;
    try {
      saved = raw && JSON.parse(raw);
    } catch {
      /* Replace corrupt identity. */
    }
    if (saved?.url === connection.url && saved?.token === connection.token)
      return saved.namespace;
    await marketCache.clear();
    const namespace =
      Date.now().toString(36) + Math.random().toString(36).slice(2);
    const value = JSON.stringify({ ...connection, namespace });
    // Credentials never enter AsyncStorage or persistent cache keys.
    if (Platform.OS === "web") sessionStorage.setItem(key, value);
    else await SecureStore.setItemAsync(key, value);
    return namespace;
  });
}
export function clearMarketCache() {
  const clearing = marketCache.clear();
  return serial(async () => {
    await clearing;
    if (Platform.OS === "web") sessionStorage.removeItem(key);
    else await SecureStore.deleteItemAsync(key);
  });
}
