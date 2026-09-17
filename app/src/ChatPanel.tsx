import { deviceAdminConnection } from "./deviceReadingStorage";
import MathText from "./MathText";
import React, { useEffect, useRef, useState } from "react";
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Linking,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, APIError } from "./api";
import type { Connection } from "./types";
import { C, s } from "./theme";
import { tokenAmount } from "./usageView";

type Session = {
  id: string;
  title: string;
  profile: string;
  updated_at: string;
};
type Message = {
  id: string;
  question: string;
  answer: string;
  profile: string;
  status: string;
  failure: string;
  context_at: string;
  tokens: Record<string, number>;
  references: { id: string; title: string; url: string }[];
};
type Overview = {
  enabled: boolean;
  worker_online?: boolean;
  sessions: Session[];
  models: { id: string; name: string; model: string; effort: string }[];
};
type Detail = Session & { messages: Message[] };
const labels: Record<string, string> = {
  standard: "Sol · medium",
  confirmation: "Astra · light",
  adjudication: "Astra · medium",
};
const states: Record<string, string> = {
  queued: "排队中",
  running: "正在处理任务…",
  outcome_unknown: "结果未知",
  error: "未完成",
  expired: "排队已超时",
};
const time = (v: string) =>
  v
    ? new Date(v).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "";
const retry = (count: number, error: Error) =>
  !(error instanceof APIError && [401, 403, 404].includes(error.status)) &&
  count < 1;

export default function ChatPanel({ connection }: { connection: Connection }) {
  if (connection.demo)
    return (
      <View style={h.empty}>
        <Text style={s.h2}>和你的雷达聊一聊</Text>
        <Text style={s.body}>
          连接服务器后，可分析已采集资料、检查服务状态。
        </Text>
      </View>
    );
  return (
    <ChatConnection
      key={connection.url + connection.token}
      connection={connection}
    />
  );
}

function ChatConnection({
  connection: baseConnection,
}: {
  connection: Connection;
}) {
  const [connection, setConnection] = useState(baseConnection);
  useEffect(() => {
    let alive = true;
    void deviceAdminConnection(baseConnection)
      .then((c) => {
        if (alive) setConnection(c);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [baseConnection.url, baseConnection.token]);
  const client = useQueryClient();
  const key = ["analysis-chat", connection.url, connection.token];
  const [sid, setSid] = useState<string>(
    () => client.getQueryData([...key, "selected"]) || "",
  );
  const [profile, setProfile] = useState("standard");
  const loadedProfile = useRef("");
  const [text, setText] = useState("");
  const [list, setList] = useState(false);
  const [linkError, setLinkError] = useState("");
  const request = useRef<{
    id: string;
    question: string;
    profile: string;
    sid: string;
  } | null>(null);
  const scroll = useRef<ScrollView>(null);
  const overview = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => api<Overview>(connection, "/v1/chat", { signal }),
    refetchInterval: 15000,
    retry,
  });
  const selected = sid || overview.data?.sessions[0]?.id || "";
  const detailKey = [...key, "session", selected];
  const detail = useQuery({
    queryKey: detailKey,
    queryFn: ({ signal }) =>
      api<Detail>(connection, `/v1/chat/sessions/${selected}`, { signal }),
    enabled: !!selected,
    refetchInterval: (query) =>
      query.state.data?.messages.some((m) =>
        ["queued", "running"].includes(m.status),
      )
        ? 2000
        : 10000,
    retry,
  });
  useEffect(() => {
    if (detail.data && loadedProfile.current !== detail.data.id) {
      loadedProfile.current = detail.data.id;
      setProfile(detail.data.profile);
    }
  }, [detail.data?.id, detail.data?.profile]);
  const choose = (value: Session) => {
    loadedProfile.current = value.id;
    setSid(value.id);
    setProfile(value.profile);
    client.setQueryData([...key, "selected"], value.id);
    setList(false);
    setText("");
    request.current = null;
  };
  const create = useMutation({
    mutationFn: () =>
      api<Session>(connection, "/v1/chat/sessions", { method: "POST" }),
    retry: false,
    onSuccess: (value) => {
      choose(value);
      void client.invalidateQueries({ queryKey: key });
    },
  });
  const send = useMutation({
    mutationFn: async () => {
      const question = text.trim();
      if (!question) throw new Error("请输入消息");
      let target = selected;
      if (!target) {
        const session = await api<Session>(connection, "/v1/chat/sessions", {
          method: "POST",
        });
        target = session.id;
        setSid(target);
        client.setQueryData([...key, "selected"], target);
      }
      if (
        !request.current ||
        request.current.question !== question ||
        request.current.profile !== profile ||
        request.current.sid !== target
      ) {
        request.current = {
          id: `chat_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`,
          question,
          profile,
          sid: target,
        };
      }
      const pending = request.current;
      return api<Message>(connection, `/v1/chat/sessions/${target}/messages`, {
        method: "POST",
        body: JSON.stringify({
          id: pending.id,
          question: pending.question,
          profile: pending.profile,
        }),
      });
    },
    retry: false,
    onSuccess: () => {
      setText("");
      request.current = null;
      void client.invalidateQueries({ queryKey: key });
      setTimeout(() => scroll.current?.scrollToEnd({ animated: true }), 100);
    },
  });
  const denied = [overview.error, detail.error].some(
    (e) => e instanceof APIError && [401, 403].includes(e.status),
  );
  const messages = denied ? [] : detail.data?.messages || [];
  const error = overview.error || detail.error || create.error || send.error;
  const busy = send.isPending || create.isPending;
  return (
    <KeyboardAvoidingView
      style={{ flex: 1 }}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <View style={h.heading}>
        <View style={{ flex: 1 }}>
          <Text style={h.eyebrow}>CODEX / 通用助手</Text>
          <Text style={h.title} numberOfLines={1}>
            {detail.data?.title || "和你的雷达聊一聊"}
          </Text>
        </View>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="切换会话"
          onPress={() => setList(true)}
          style={h.small}
        >
          <Text style={h.action}>会话</Text>
        </Pressable>
        <Pressable
          accessibilityRole="button"
          disabled={busy || !overview.data?.enabled}
          onPress={() => create.mutate()}
          style={h.small}
        >
          <Text style={h.action}>＋ 新建</Text>
        </Pressable>
      </View>
      <View style={h.models}>
        {(overview.data?.models || []).map((m) => (
          <Pressable
            key={m.id}
            accessibilityRole="button"
            accessibilityState={{ selected: profile === m.id }}
            disabled={busy}
            onPress={() => {
              loadedProfile.current = selected;
              setProfile(m.id);
            }}
            style={[h.model, profile === m.id && h.chosen]}
          >
            <Text
              style={[h.modelText, profile === m.id && { color: C.accent }]}
            >
              {labels[m.id] || m.name}
            </Text>
          </Pressable>
        ))}
      </View>
      <Text style={h.caption}>数据分析 · 服务诊断与修复 · 通用任务</Text>
      {error && (
        <View style={h.error}>
          <Text style={s.body}>
            {error instanceof Error ? error.message : "连接暂不可用"}
          </Text>
          <Pressable
            accessibilityRole="button"
            onPress={() => {
              void overview.refetch();
              void detail.refetch();
            }}
          >
            <Text style={h.action}>刷新状态</Text>
          </Pressable>
        </View>
      )}
      {overview.data?.worker_online === false && (
        <Text style={s.muted}>
          对话执行进程暂时离线，消息不会丢失；请等待恢复。
        </Text>
      )}
      {denied && (
        <Text style={s.muted}>
          运维对话需要管理令牌，请在「网页采集中心 →
          设备管理权限」保存后重新进入 Chat。
        </Text>
      )}
      {overview.data && !overview.data.enabled && (
        <Text style={h.error}>服务器尚未启用 Codex 对话。</Text>
      )}
      <ScrollView
        ref={scroll}
        style={{ flex: 1 }}
        contentContainerStyle={h.messages}
        keyboardShouldPersistTaps="handled"
      >
        {detail.isLoading && <ActivityIndicator color={C.accent} />}
        {!messages.length && (
          <View style={h.welcome}>
            <Text style={s.h2}>从一个问题开始。</Text>
            <Text style={[s.body, { color: C.muted, marginTop: 8 }]}>
              把资料串起来，或看看雷达运行得怎样。
            </Text>
            {[
              "检查当前采集和翻译任务，有哪些问题？",
              "最近国内算力产业有哪些新证据？",
              "总结当前行业判断与仍缺少的数据。",
            ].map((q) => (
              <Pressable
                key={q}
                onPress={() => setText(q)}
                style={h.suggestion}
              >
                <Text style={s.body}>{q} ↗</Text>
              </Pressable>
            ))}
          </View>
        )}
        {messages.map((m) => (
          <View key={m.id} style={{ marginBottom: 24 }}>
            <View style={h.user}>
              <Text selectable style={s.body}>
                {m.question}
              </Text>
            </View>
            <View style={h.answer}>
              <Text style={h.eyebrow}>
                {labels[m.profile] || m.profile}{" "}
                {m.context_at ? ` · ${time(m.context_at)}` : ""}
              </Text>
              {m.answer ? (
                <MathText text={m.answer} style={[s.body, { marginTop: 8 }]} />
              ) : (
                <View style={[s.row, { gap: 8, marginTop: 10 }]}>
                  {["queued", "running"].includes(m.status) && (
                    <ActivityIndicator size="small" color={C.accent} />
                  )}
                  <Text style={s.muted}>
                    {m.failure || states[m.status] || m.status}
                  </Text>
                </View>
              )}
              {m.references.map((r, i) => (
                <Pressable
                  key={r.id}
                  accessibilityRole="link"
                  onPress={() => {
                    if (/^https:\/\//.test(r.url))
                      void Linking.openURL(r.url).catch(() =>
                        setLinkError("无法打开来源链接"),
                      );
                  }}
                  style={h.reference}
                >
                  <Text numberOfLines={2} style={h.referenceText}>
                    {i + 1}. {r.title} ↗
                  </Text>
                </Pressable>
              ))}
              {m.tokens.input_tokens !== undefined && (
                <Text style={[s.muted, { marginTop: 10 }]}>
                  输入 {tokenAmount(m.tokens.input_tokens)} · 输出{" "}
                  {tokenAmount(m.tokens.output_tokens || 0)} tokens
                </Text>
              )}
            </View>
          </View>
        ))}
        {linkError && <Text style={s.muted}>{linkError}</Text>}
      </ScrollView>
      <View style={h.composer}>
        <TextInput
          accessibilityLabel="对话消息"
          value={text}
          onChangeText={setText}
          placeholder="提问，或让 Codex 检查、修复服务…"
          placeholderTextColor={C.muted}
          multiline
          maxLength={6000}
          editable={!busy}
          style={h.input}
        />
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="发送消息"
          disabled={!text.trim() || busy || denied || !overview.data?.enabled}
          onPress={() => send.mutate()}
          style={[
            h.send,
            (!text.trim() || busy || !overview.data?.enabled) && {
              opacity: 0.4,
            },
          ]}
        >
          {send.isPending ? (
            <ActivityIndicator color="white" />
          ) : (
            <Text style={{ color: "white", fontWeight: "700" }}>发送 ↑</Text>
          )}
        </Pressable>
      </View>
      <Modal
        visible={list}
        animationType="slide"
        onRequestClose={() => setList(false)}
      >
        <SafeAreaView style={s.screen}>
          <View style={h.heading}>
            <Text style={[s.h2, { flex: 1 }]}>你的会话</Text>
            <Pressable
              accessibilityRole="button"
              onPress={() => setList(false)}
              style={h.small}
            >
              <Text style={h.action}>完成</Text>
            </Pressable>
          </View>
          <ScrollView contentContainerStyle={h.messages}>
            {(denied ? [] : overview.data?.sessions || []).map((v) => (
              <Pressable
                key={v.id}
                onPress={() => choose(v)}
                style={[
                  h.session,
                  selected === v.id && { borderColor: C.accent },
                ]}
              >
                <Text numberOfLines={2} style={s.body}>
                  {v.title}
                </Text>
                <Text style={s.muted}>
                  {time(v.updated_at)} · {labels[v.profile]}
                </Text>
              </Pressable>
            ))}
          </ScrollView>
        </SafeAreaView>
      </Modal>
    </KeyboardAvoidingView>
  );
}
const h = StyleSheet.create({
  heading: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 20,
    paddingTop: 8,
    paddingBottom: 14,
    gap: 8,
  },
  eyebrow: {
    fontSize: 10,
    letterSpacing: 1,
    color: C.muted,
    fontWeight: "600",
  },
  title: { fontSize: 21, fontWeight: "700", color: C.ink, marginTop: 6 },
  small: { padding: 8 },
  action: { fontSize: 13, color: C.accent, fontWeight: "600" },
  models: { flexDirection: "row", gap: 6, paddingHorizontal: 20 },
  model: {
    flex: 1,
    paddingVertical: 10,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: C.line,
    alignItems: "center",
    backgroundColor: C.paper,
  },
  chosen: { borderColor: C.accent, backgroundColor: C.pale },
  modelText: { fontSize: 11, color: C.muted, fontWeight: "600" },
  caption: {
    fontSize: 11,
    color: C.muted,
    paddingHorizontal: 20,
    paddingVertical: 12,
  },
  messages: { paddingHorizontal: 20, paddingBottom: 20 },
  welcome: { paddingTop: 26, paddingBottom: 35 },
  suggestion: {
    borderBottomWidth: 1,
    borderColor: C.line,
    paddingVertical: 18,
  },
  user: {
    alignSelf: "flex-end",
    maxWidth: "92%",
    backgroundColor: C.pale,
    borderRadius: 16,
    borderBottomRightRadius: 4,
    padding: 14,
    marginBottom: 18,
  },
  answer: { paddingHorizontal: 2 },
  reference: {
    paddingVertical: 9,
    marginTop: 6,
    borderTopWidth: 1,
    borderColor: C.line,
  },
  referenceText: { fontSize: 12, lineHeight: 19, color: C.green },
  composer: {
    flexDirection: "row",
    alignItems: "flex-end",
    padding: 14,
    borderTopWidth: 1,
    borderColor: C.line,
    gap: 10,
    backgroundColor: C.paper,
  },
  input: {
    flex: 1,
    fontSize: 15,
    color: C.ink,
    minHeight: 44,
    maxHeight: 130,
    padding: 10,
    borderRadius: 12,
    backgroundColor: C.bg,
    textAlignVertical: "top",
  },
  send: {
    backgroundColor: C.accent,
    borderRadius: 12,
    minHeight: 44,
    padding: 12,
    justifyContent: "center",
  },
  session: {
    backgroundColor: C.paper,
    padding: 16,
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 14,
    marginBottom: 10,
    gap: 6,
  },
  empty: { padding: 24, gap: 12 },
  error: { padding: 12, backgroundColor: C.pale, margin: 12, borderRadius: 10 },
});
