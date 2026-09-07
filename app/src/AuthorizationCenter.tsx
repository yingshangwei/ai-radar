import React, { useEffect, useState } from "react";
import {
  ActivityIndicator,
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

type Site = {
  domain: string;
  total: number;
  pending: number;
  document_id: string;
  url: string;
  enabled: boolean;
  status: string;
  message: string;
  verified_at: string;
  statuses: Record<string, number>;
};
type Sites = {
  enabled: boolean;
  items: Site[];
  active: { id: string; domain: string; expires_at: number }[];
};
type Session = { id: string; path: string; domain: string };
const LABEL: Record<string, string> = {
  ready: "已验证可读取",
  unverified: "尚未验证",
  auth_required: "需要登录",
  access_restricted: "访问受限",
  rate_limited: "访问频率受限",
  unavailable: "暂时无法读取",
  pending: "等待验证",
};
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
    void refresh();
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
      setNotice("管理授权已保存在此设备，可以打开网站了。");
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
        else onClose();
      }}
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
            <Text style={s.label}>{remote ? "远程浏览器" : "你的雷达"}</Text>
            <Text style={[s.sectionTitle, { marginTop: 5 }]}>
              {remote ? remote.domain : "网页授权中心"}
            </Text>
          </View>
          <Pressable
            accessibilityRole="button"
            onPress={() => {
              if (remote) void closeRemote();
              else onClose();
            }}
            style={s.smallButton}
          >
            <Text style={s.body}>{remote ? "关闭" : "返回"}</Text>
          </Pressable>
        </View>
        {remote ? (
          <RemoteBrowser
            url={connection.url + remote.path}
            onDone={() => {
              setRemote(undefined);
              void refresh();
              onRefresh();
            }}
          />
        ) : (
          <ScrollView
            contentContainerStyle={{ padding: 22, paddingBottom: 50 }}
            keyboardShouldPersistTaps="handled"
          >
            <View style={[s.note, { marginBottom: 20 }]}>
              <Text style={s.body}>
                在这里完成登录或网页验证，后续采集自动复用。
              </Text>
              <Text style={[s.muted, { marginTop: 8 }]}>
                打开的是采集服务的专用浏览器。操作完成后点击“验证并补采”，我们会检查目标文章能否读取。每次可操作一个网站，窗口有效期为
                20 分钟。
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
              <ActivityIndicator color={C.green} style={{ marginBottom: 16 }} />
            )}
            {data && !data.enabled && (
              <Text style={[s.body, { marginBottom: 20 }]}>
                网页授权服务尚未启用，请稍后刷新。
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
            {data?.items.map((site) => (
              <View key={site.domain} style={s.card}>
                <View style={s.spread}>
                  <Text style={[s.cardTitle, { fontSize: 19, flex: 1 }]}>
                    {site.domain}
                  </Text>
                  <Text
                    style={[
                      s.muted,
                      {
                        color:
                          site.enabled && site.status === "ready"
                            ? C.green
                            : C.accent,
                      },
                    ]}
                  >
                    {site.status === "ready" && !site.enabled
                      ? "自动补采已暂停"
                      : LABEL[site.status] || "等待处理"}
                  </Text>
                </View>
                <Text style={[s.muted, { marginTop: 8 }]}>
                  {site.total} 个直接来源 · {site.pending} 个尚未取得正文
                </Text>
                <Text style={[s.body, { marginTop: 10, fontSize: 13 }]}>
                  {site.message}
                </Text>
                {site.status === "unverified" &&
                  !!(
                    site.statuses.auth_required ||
                    site.statuses.access_restricted
                  ) && (
                    <Text style={[s.muted, { marginTop: 8 }]}>
                      网站可能限制自动访问，打开后可确认是否需要登录。
                    </Text>
                  )}
                <Pressable
                  disabled={busy || !data.enabled}
                  style={[
                    s.button,
                    { marginTop: 16, opacity: data.enabled ? 1 : 0.4 },
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
                  <Text style={s.buttonText}>
                    {site.pending ? "打开网页处理" : "打开浏览器"}
                  </Text>
                </Pressable>
                {site.status === "ready" && (
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
                            body: JSON.stringify({ enabled: !site.enabled }),
                          },
                        );
                      })
                    }
                  >
                    <Text style={s.body}>
                      {site.enabled ? "暂停自动补采" : "恢复自动补采"}
                    </Text>
                  </Pressable>
                )}
              </View>
            ))}
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
    </Modal>
  );
}
