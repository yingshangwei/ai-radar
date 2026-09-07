import { Platform } from "react-native";
import * as SecureStore from "expo-secure-store";
import AsyncStorage from "@react-native-async-storage/async-storage";
import type { Connection } from "./types";

const KEY = "airadar.connection.v1";
export const storage = {
  async load(): Promise<Connection | null> {
    const value =
      Platform.OS === "web"
        ? sessionStorage.getItem(KEY)
        : await SecureStore.getItemAsync(KEY);
    if (!value) return null;
    try {
      return JSON.parse(value);
    } catch {
      return null;
    }
  },
  async save(connection: Connection) {
    if (Platform.OS === "web")
      sessionStorage.setItem(KEY, JSON.stringify(connection));
    else await SecureStore.setItemAsync(KEY, JSON.stringify(connection));
  },
  async clear() {
    if (Platform.OS === "web") sessionStorage.removeItem(KEY);
    else await SecureStore.deleteItemAsync(KEY);
    if (Platform.OS === "web") sessionStorage.removeItem("airadar.admin.v1");
    else await SecureStore.deleteItemAsync("airadar.admin.v1");
    const keys = (await AsyncStorage.getAllKeys()).filter((k) =>
      k.startsWith("airadar.cache."),
    );
    await AsyncStorage.multiRemove(keys);
  },
};

export function normalizeURL(input: string) {
  const url = new URL(input.trim());
  const local = ["localhost", "127.0.0.1", "10.0.2.2", "[::1]"].includes(
    url.hostname,
  );
  if (
    (url.protocol !== "https:" && !(local && url.protocol === "http:")) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  )
    throw new Error(
      "请输入 HTTPS 服务地址。本机调试可使用 http://localhost 或 http://10.0.2.2。",
    );
  return url.toString().replace(/\/$/, "");
}

export class APIError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export async function api<T>(
  connection: Connection,
  path: string,
  options: RequestInit = {},
  timeoutMs = 18000,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${connection.url}${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${connection.token}`,
      },
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new APIError(
        response.status,
        typeof body.detail === "string"
          ? body.detail
          : `请求失败 (${response.status})`,
      );
    }
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof APIError) throw error;
    throw new Error("暂时无法连接服务，请检查网络与服务地址。");
  } finally {
    clearTimeout(timer);
  }
}

export async function cached<T>(
  connection: Connection,
  path: string,
): Promise<{ data: T; offline: boolean }> {
  const key = `airadar.cache.${connection.url}${path}`;
  try {
    const data = await api<T>(connection, path);
    try {
      await AsyncStorage.setItem(key, JSON.stringify(data));
    } catch {
      // A full device cache must not discard a successfully fetched document.
      // Offline access remains available for entries that were actually saved.
    }
    return { data, offline: false };
  } catch (error) {
    // Never hide expired credentials behind cached private data.
    if (error instanceof APIError) throw error;
    const data = await AsyncStorage.getItem(key);
    if (data) return { data: JSON.parse(data), offline: true };
    throw error;
  }
}
