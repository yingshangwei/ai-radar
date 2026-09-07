import AsyncStorage from "@react-native-async-storage/async-storage";
import * as SecureStore from "expo-secure-store";
import type { Connection } from "./types";
import { siteHost, type DeviceSitePermissions } from "./deviceReadingPolicy";

const listeners = new Set<() => void>();
let writes: Promise<unknown> = Promise.resolve();
const key = (server: string, suffix = "sites") =>
  `airadar.deviceReading.${suffix}.${encodeURIComponent(server)}`;

export function subscribeDeviceReading(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
export async function loadDeviceSites(
  server: string,
): Promise<DeviceSitePermissions> {
  try {
    const data = JSON.parse((await AsyncStorage.getItem(key(server))) || "{}");
    const result: DeviceSitePermissions = {};
    for (const [domain, value] of Object.entries(data)) {
      if (!value || typeof value !== "object") continue;
      const item = value as Record<string, unknown>;
      const normalized = siteHost(domain);
      if (normalized && typeof item.allowed === "boolean")
        result[normalized] = {
          allowed: item.allowed,
          needsVerification: item.needsVerification === true,
          updatedAt: Number(item.updatedAt) || 0,
        };
    }
    return result;
  } catch {
    return {};
  }
}
export async function updateDeviceSite(
  server: string,
  domain: string,
  patch: { allowed?: boolean; needsVerification?: boolean },
): Promise<void> {
  const next = writes
    .catch(() => {})
    .then(async () => {
      const permissions = await loadDeviceSites(server);
      const normalized = siteHost(domain);
      if (!normalized) throw new Error("网站地址无效。");
      permissions[normalized] = {
        allowed: false,
        needsVerification: false,
        ...permissions[normalized],
        ...patch,
        updatedAt: Date.now(),
      };
      await AsyncStorage.setItem(key(server), JSON.stringify(permissions));
      listeners.forEach((listener) => listener());
    });
  writes = next;
  await next;
}
export async function loadDeviceDeferrals(
  server: string,
): Promise<Record<string, number>> {
  try {
    return JSON.parse(
      (await AsyncStorage.getItem(key(server, "retry"))) || "{}",
    );
  } catch {
    return {};
  }
}
export async function deferDeviceDocument(
  server: string,
  id: string,
): Promise<void> {
  const values = await loadDeviceDeferrals(server);
  const now = Date.now();
  const next = Object.fromEntries(
    Object.entries(values).filter(
      ([, time]) => typeof time === "number" && time > now,
    ),
  );
  next[id] = now + 30 * 60 * 1000;
  await AsyncStorage.setItem(key(server, "retry"), JSON.stringify(next));
}
export async function deviceAdminConnection(
  connection: Connection,
): Promise<Connection> {
  try {
    const saved = JSON.parse(
      (await SecureStore.getItemAsync("airadar.admin.v1")) || "null",
    );
    if (saved?.url === connection.url && typeof saved.token === "string")
      return { ...connection, token: saved.token };
  } catch {}
  return connection;
}
