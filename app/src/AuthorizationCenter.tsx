import React, { useEffect, useState } from "react";
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  Text,
  TextInput,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import * as SecureStore from "expo-secure-store";
import { api, APIError } from "./api";
import { C, s } from "./theme";
import type { Connection } from "./types";
import RemoteBrowser from "./RemoteBrowser";
import LocalBrowser from "./LocalBrowser";
import { sitePresentation, type Site } from "./siteState";
import { siteHost, type DeviceSitePermissions } from "./deviceReadingPolicy";
import {
  loadDeviceSites,
  subscribeDeviceReading,
  updateDeviceSite,
} from "./deviceReadingStorage";

type Sites = {
  enabled: boolean;
  items: Site[];
  active: { id: string; domain: string; expires_at: number }[];
};
type Session = { id: string; path: string; domain: string };
const errorText = (e: unknown) =>
  e instanceof Error ? e.message : "操作未完成，请稍后重试。";
const KEY = "airadar.admin.v1";

export default function AuthorizationCenter({
  connection,
  open,
  onClose,
  onRefresh,
}: {
  connection: Connection;
  open: boolean;
  onClose: () => void;
  onRefresh: () => void;
}) {
  const [data, setData] = useState<Sites>();
  const [admin, setAdmin] = useState("");
  const [draft, setDraft] = useState("");
  const [needsAdmin, setNeedsAdmin] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [remote, setRemote] = useState<Session>();
  const [local, setLocal] = useState<Site>();
  const [consent, setConsent] = useState<Site>();
  const [deviceSites, setDeviceSites] = useState<DeviceSitePermissions>({});
  const [notice, setNotice] = useState("");
  const adminConnection = { ...connection, token: admin || connection.token };
  const refresh = async () => {
    try {
      setData(await api<Sites>(connection, "/v1/browser/sites"));
    } catch (e) {
      setError(errorText(e));
    }
  };
  useEffect(() => {
    if (!open) return;
    setAdmin("");
    void refresh();
    void loadDeviceSites(connection.url).then(setDeviceSites);
    const unsubscribe = subscribeDeviceReading(() => {
      void loadDeviceSites(connection.url).then(setDeviceSites);
    });
    let cancelled = false;
    void (async () => {
      const saved =
        Platform.OS === "web"
          ? sessionStorage.getItem(KEY)
          : await SecureStore.getItemAsync(KEY);
      if (!saved || cancelled) return;
      try {
        const value = JSON.parse(saved);
        if (value.url === connection.url) setAdmin(value.token);
      } catch (_) {}
    })();
    const timer = setInterval(() => void refresh(), 15000);
    return () => {
      cancelled = true;
      unsubscribe();
      clearInterval(timer);
    };
  }, [open, connection.url]);
  const action = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await fn();
    } catch (e) {
      if (e instanceof APIError && e.status === 403) setNeedsAdmin(true);
      setError(errorText(e));
    } finally {
      setBusy(false);
      void refresh();
    }
  };
  const saveAdmin = () =>
    action(async () => {
      const token = draft.trim();
      await api({ ...connection, token }, "/v1/browser/permissions");
      const value = JSON.stringify({ url: connection.url, token });
      if (Platform.OS === "web") sessionStorage.setItem(KEY, value);
      else await SecureStore.setItemAsync(KEY, value);
      setAdmin(token);
      setDraft("");
      setNeedsAdmin(false);
      setNotice("管理权限已保存在此设备，可以读取并提交正文了。");
    });
  const closeRemote = async () => {
    if (remote)
      try {
        await api(adminConnection, `/v1/browser/sessions/${remote.id}/close`, {
          method: "POST",
        });
      } catch (_) {}
    setRemote(undefined);
    void refresh();
    onRefresh();
  };
  return (
    <Modal
      visible={open}
      animationType="slide"
      onRequestClose={() => {
        if (remote) void closeRemote();
        else if (local) setLocal(undefined);
        else if (consent) setConsent(undefined);
        else onClose();
      }}
    >
      <KeyboardAvoidingView
        style={{ flex: 1 }}
        behavior={Platform.OS === "ios" ? "padding" : "height"}
      >
        <SafeAreaView style={{ flex: 1, backgroundColor: C.paper }}>
          <View
            style={[
              s.spread,
              {
                paddingHorizontal: 22,
                paddingVertical: 16,
                borderBottomWidth: 1,
                borderColor: C.line,
              },
            ]}
          >
            <View>
              <Text style={s.label}>
                {remote ? "服务器浏览器" : local ? "手机浏览器" : "你的雷达"}
              </Text>
              <Text style={[s.sectionTitle, { marginTop: 5 }]}>
                {remote ? remote.domain : local ? local.domain : "网页采集中心"}
              </Text>
            </View>
            <Pressable
              accessibilityRole="button"
              onPress={() => {
                if (remote) void closeRemote();
                else if (local) setLocal(undefined);
                else if (consent) setConsent(undefined);
                else onClose();
              }}
              style={s.smallButton}
            >
              <Text style={s.body}>{remote || local ? "关闭" : "返回"}</Text>
            </Pressable>
          </View>
          {consent ? (
            <ScrollView contentContainerStyle={{ padding: 24, gap: 18 }}>
              <Text style={s.h2}>让手机自动读取 {consent.domain}</Text>
              <Text style={s.body}>
                授权手机自动读取此网站，仅上传文章正文。
              </Text>
              <Text style={s.muted}>
                你只需完成一次网站登录或人机验证。此后在 App
                前台，手机会自动读取这个网站的待处理文章，并交给服务器翻译和总结，无需逐篇确认。
              </Text>
              <Text style={s.muted}>
                许可仅适用于此网站及 www 别名；不读取密码、不上传 Cookie
                或登录状态。你可以随时暂停手机补采。App
                关闭或进入后台时停止运行。
              </Text>
              <Pressable
                accessibilityRole="button"
                disabled={busy}
                style={s.button}
                onPress={() =>
                  action(async () => {
                    await updateDeviceSite(connection.url, consent.domain, {
                      allowed: true,
                      needsVerification: false,
                    });
                    setLocal(consent);
                    setConsent(undefined);
                  })
                }
              >
                <Text style={s.buttonText}>允许并打开网站</Text>
              </Pressable>
            </ScrollView>
          ) : local ? (
            <LocalBrowser
              connection={adminConnection}
              document={local}
              onDone={(message) => {
                setLocal(undefined);
                setNotice(message);
                void refresh();
                onRefresh();
                onClose();
              }}
            />
          ) : remote ? (
            <View style={{ flex: 1 }}>
              <Text
                style={[
                  s.muted,
                  {
                    paddingHorizontal: 18,
                    paddingVertical: 10,
                    backgroundColor: "#EEEFE6",
                  },
                ]}
              >
                使用腾讯云服务器网络。若人机验证反复失败，请返回改用“手机读取”。
              </Text>
              <RemoteBrowser
                url={connection.url + remote.path}
                onDone={() => {
                  setRemote(undefined);
                  void refresh();
                  onRefresh();
                }}
              />
            </View>
          ) : (
            <ScrollView
              contentContainerStyle={{ padding: 22, paddingBottom: 50 }}
              keyboardShouldPersistTaps="handled"
            >
              <View style={[s.note, { marginBottom: 20 }]}>
                <Text style={s.body}>
                  公开网页自动采集，你只需关注需要处理的来源。
                </Text>
                <Text style={[s.muted, { marginTop: 8 }]}>
                  正文已采集的网页无需处理。受限网站只需在手机授权一次，之后 App
                  在前台时会自动补采、翻译和解读，不用逐篇点采集。
                </Text>
              </View>
              {(needsAdmin || !admin) && (
                <View style={[s.note, { marginBottom: 20 }]}>
                  <Text style={s.label}>设备管理权限</Text>
                  <Text style={[s.muted, { marginVertical: 8 }]}>
                    当前连接若使用阅读令牌，首次操作需填写管理令牌。保存后无需每次输入。
                  </Text>
                  <TextInput
                    style={s.input}
                    value={draft}
                    onChangeText={setDraft}
                    secureTextEntry
                    autoCapitalize="none"
                    autoCorrect={false}
                    placeholder="管理令牌（已用管理令牌连接可直接操作）"
                    accessibilityLabel="管理令牌"
                  />
                  <Pressable
                    disabled={busy || !draft.trim()}
                    onPress={saveAdmin}
                    style={[
                      s.smallButton,
                      { marginTop: 10, opacity: draft.trim() ? 1 : 0.4 },
                    ]}
                  >
                    <Text style={s.body}>验证并保存</Text>
                  </Pressable>
                </View>
              )}
              {!!error && (
                <Text
                  accessibilityRole="alert"
                  style={[s.body, { color: C.accent, marginBottom: 16 }]}
                >
                  {error}
                </Text>
              )}
              {!!notice && (
                <Text style={[s.body, { color: C.green, marginBottom: 16 }]}>
                  {notice}
                </Text>
              )}
              {busy && (
                <ActivityIndicator
                  color={C.green}
                  style={{ marginBottom: 16 }}
                />
              )}
              {data && !data.enabled && (
                <Text style={[s.body, { marginBottom: 20 }]}>
                  服务器浏览器暂不可用，仍可使用手机读取和提交正文。
                </Text>
              )}
              {data?.active.map((active) => (
                <View key={active.id} style={s.note}>
                  <Text style={s.body}>{active.domain} 的授权窗口仍在运行</Text>
                  <Pressable
                    disabled={busy}
                    style={[s.smallButton, { marginTop: 10 }]}
                    onPress={() =>
                      action(async () => {
                        await api(
                          adminConnection,
                          `/v1/browser/sessions/${active.id}/close`,
                          { method: "POST" },
                        );
                      })
                    }
                  >
                    <Text style={s.body}>关闭旧窗口</Text>
                  </Pressable>
                </View>
              ))}
              {data?.items.map((site) => {
                const display = sitePresentation(site);
                const grant = deviceSites[siteHost(site.domain)];
                const needsReading =
                  site.status !== "blocked" &&
                  ["login", "restricted", "retry"].includes(display.action);
                return (
                  <View key={site.domain} style={s.card}>
                    <View style={s.spread}>
                      <Text
                        style={[
                          s.cardTitle,
                          { fontSize: 19, flex: 1, marginRight: 8 },
                        ]}
                      >
                        {site.domain}
                      </Text>
                      <Text
                        style={[
                          s.muted,
                          {
                            color:
                              display.action === "none" ||
                              display.action === "automatic"
                                ? C.green
                                : C.accent,
                          },
                        ]}
                      >
                        {display.label}
                      </Text>
                    </View>
                    <Text style={[s.muted, { marginTop: 8 }]}>
                      {Math.max(0, site.total - site.pending)} / {site.total}{" "}
                      个来源已取得正文
                      {site.pending ? ` · ${site.pending} 个待处理` : ""}
                    </Text>
                    <Text style={[s.body, { marginTop: 10, fontSize: 13 }]}>
                      {display.message}
                    </Text>
                    {site.retry_at && display.action === "retry" && (
                      <Text style={[s.muted, { marginTop: 6 }]}>
                        下次重试：
                        {new Date(site.retry_at).toLocaleString("zh-CN")}
                      </Text>
                    )}
                    {needsReading && (
                      <>
                        <Pressable
                          accessibilityRole="button"
                          disabled={busy}
                          style={[
                            s.button,
                            { marginTop: 16, opacity: busy ? 0.5 : 1 },
                          ]}
                          onPress={() =>
                            action(async () => {
                              await api(
                                adminConnection,
                                "/v1/browser/permissions",
                              );
                              const saved = await loadDeviceSites(
                                connection.url,
                              );
                              if (
                                Platform.OS === "web" ||
                                saved[siteHost(site.domain)]?.allowed
                              )
                                setLocal(site);
                              else setConsent(site);
                            })
                          }
                        >
                          <Text style={s.buttonText}>
                            {Platform.OS === "web"
                              ? "打开原文 / 粘贴正文"
                              : grant?.needsVerification
                                ? "重新验证手机访问"
                                : grant?.allowed
                                  ? "打开手机浏览器"
                                  : "开启手机自动读取"}
                          </Text>
                        </Pressable>
                        {data.enabled && (
                          <Pressable
                            accessibilityRole="button"
                            disabled={busy}
                            style={[
                              s.smallButton,
                              { marginTop: 10, alignItems: "center" },
                            ]}
                            onPress={() =>
                              action(async () => {
                                const session = await api<Session>(
                                  adminConnection,
                                  "/v1/browser/sessions",
                                  {
                                    method: "POST",
                                    body: JSON.stringify({
                                      document_id: site.document_id,
                                    }),
                                  },
                                  90000,
                                );
                                setRemote(session);
                              })
                            }
                          >
                            <Text style={s.muted}>服务器浏览器（可选）</Text>
                          </Pressable>
                        )}
                      </>
                    )}
                    {grant?.allowed && Platform.OS !== "web" && (
                      <View style={{ marginTop: 12, gap: 6 }}>
                        <Text
                          style={[
                            s.muted,
                            {
                              color: grant.needsVerification
                                ? C.accent
                                : C.green,
                            },
                          ]}
                        >
                          {grant.needsVerification
                            ? "手机访问需要重新验证，自动许可仍保留。"
                            : "手机自动补采已开启 · App 在前台时运行"}
                        </Text>
                        <Pressable
                          accessibilityRole="button"
                          disabled={busy}
                          onPress={() =>
                            action(async () => {
                              await updateDeviceSite(
                                connection.url,
                                site.domain,
                                { allowed: false },
                              );
                              setNotice(`${site.domain} 的手机补采已暂停。`);
                            })
                          }
                        >
                          <Text style={s.muted}>暂停手机补采</Text>
                        </Pressable>
                      </View>
                    )}
                    {site.status === "paused" && (
                      <Pressable
                        disabled={busy}
                        style={[s.smallButton, { marginTop: 10 }]}
                        onPress={() =>
                          action(async () => {
                            await api(
                              adminConnection,
                              `/v1/browser/sites/${encodeURIComponent(site.domain)}`,
                              {
                                method: "POST",
                                body: JSON.stringify({ enabled: true }),
                              },
                            );
                          })
                        }
                      >
                        <Text style={s.body}>恢复自动读取</Text>
                      </Pressable>
                    )}
                  </View>
                );
              })}
              {!data && !error && <ActivityIndicator color={C.green} />}
              {data?.items.length === 0 && (
                <Text style={s.muted}>
                  采集到网页来源后，会在这里按网站汇总。
                </Text>
              )}
              <Pressable
                style={[s.smallButton, { marginTop: 20 }]}
                onPress={() => void refresh()}
              >
                <Text style={s.body}>刷新状态</Text>
              </Pressable>
            </ScrollView>
          )}
        </SafeAreaView>
      </KeyboardAvoidingView>
    </Modal>
  );
}
