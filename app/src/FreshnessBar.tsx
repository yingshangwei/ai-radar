import React, { useEffect, useState } from "react";
import {
  ActivityIndicator,
  Linking,
  Modal,
  Pressable,
  ScrollView,
  Text,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { useQuery } from "@tanstack/react-query";
import Feather from "@expo/vector-icons/Feather";
import { api, cached } from "./api";
import { publicationLabel } from "./articlePresentation";
import { C, s } from "./theme";
import type { Connection, Status } from "./types";

import { unseenUpdates, type TranslationUpdate } from "./translationUpdates";

export default function FreshnessBar({
  connection,
  status,
  active,
  onRefresh,
  onOpen,
}: {
  connection: Connection;
  status?: Status;
  active: boolean;
  onRefresh: () => void;
  onOpen: (id: string) => void;
}) {
  const [pulling, setPulling] = useState(false);
  const [message, setMessage] = useState("");
  const [open, setOpen] = useState(false);
  const [seen, setSeen] = useState<string | null>(null);
  const storageKey = `airadar.cache.${connection.url}/translation-updates-seen`;
  const updates = useQuery({
    queryKey: [connection.url, "live", "translationUpdates"],
    enabled: !connection.demo && active,
    refetchInterval: active ? 30000 : false,
    queryFn: () =>
      cached<{
        items: TranslationUpdate[];
        latest_at: string | null;
        total: number;
      }>(connection, "/v1/translation-updates?limit=200"),
  });
  useEffect(() => {
    let live = true;
    AsyncStorage.getItem(storageKey)
      .then((value) => {
        if (live) setSeen(value || "");
      })
      .catch(() => {
        if (live) setSeen("");
      });
    return () => {
      live = false;
    };
  }, [storageKey]);
  useEffect(() => {
    if (seen === "" && updates.data && !updates.data.offline) {
      const initial = updates.data.data.latest_at || new Date().toISOString();
      setSeen(initial);
      void AsyncStorage.setItem(storageKey, initial).catch(() => {});
    }
  }, [seen, storageKey, updates.data]);
  const items = updates.data?.data.items || [];
  const unread = unseenUpdates(items, seen || "");
  const collecting = status?.jobs.some(
    (job) =>
      job.kind === "collect" &&
      ["queued", "running", "retrying"].includes(job.status),
  );
  const pull = async () => {
    if (connection.demo) {
      setMessage("演示模式不发起真实采集。");
      return;
    }
    setPulling(true);
    setMessage("");
    try {
      const result = await api<{ job_id: string; coalesced: boolean }>(
        connection,
        "/v1/refresh",
        { method: "POST" },
      );
      setMessage(
        result.coalesced
          ? "已合并到最近的采集任务，内容会自动更新。"
          : "已提交最新采集，原文到达即显示。",
      );
      onRefresh();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "暂时无法拉取，请稍后重试。");
    } finally {
      setPulling(false);
    }
  };
  const showArchive = () => {
    setOpen(true);
    const latest = updates.data?.data.latest_at;
    if (latest && !updates.data?.offline) {
      setSeen(latest);
      void AsyncStorage.setItem(storageKey, latest).catch(() => {});
    }
  };
  const freshness = status?.freshness;
  const xSource = status?.sources.find((source) => source.id === "x");
  const xPaymentRequired = xSource?.status === "payment_required";
  return (
    <View style={{ marginBottom: 18 }}>
      <View style={[s.spread, { gap: 10 }]}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="拉取最新数据"
          disabled={pulling || collecting}
          onPress={() => void pull()}
          style={[
            s.smallButton,
            s.row,
            {
              gap: 7,
              backgroundColor: C.ink,
              borderColor: C.ink,
              paddingVertical: 11,
            },
          ]}
        >
          {pulling || collecting ? (
            <ActivityIndicator size="small" color={C.paper} />
          ) : (
            <Feather name="refresh-cw" size={14} color={C.paper} />
          )}
          <Text style={{ color: C.paper, fontSize: 12, fontWeight: "600" }}>
            {pulling || collecting ? "正在拉取" : "拉取最新"}
          </Text>
        </Pressable>
        <Pressable
          accessibilityRole="button"
          onPress={showArchive}
          style={[s.row, { gap: 6, paddingVertical: 12 }]}
        >
          <Feather name="archive" size={15} color={C.green} />
          <Text style={{ color: C.green, fontSize: 12 }}>
            翻译归档{unread ? ` · ${unread} 条新完成` : ""}
          </Text>
        </Pressable>
      </View>
      <Text style={[s.muted, { fontSize: 11, marginTop: 10, lineHeight: 18 }]}>
        {freshness
          ? `每 ${freshness.collect_minutes} 分钟自动采集 · 原文先到，总结与翻译陆续更新`
          : "原文先到，总结与翻译陆续更新"}
      </Text>
      {xPaymentRequired && (
        <View
          style={{
            marginTop: 10,
            padding: 12,
            borderRadius: 12,
            backgroundColor: C.paper,
          }}
        >
          <Text
            accessibilityLiveRegion="polite"
            style={{ color: C.accent, fontSize: 12, lineHeight: 19 }}
          >
            X 采集已暂停：API
            余额不足或计费受限。请检查余额和消费上限，处理后自动恢复。
            {xSource.last_success_at
              ? ` 上次成功：${publicationLabel(xSource.last_success_at, false, true)}`
              : ""}
          </Text>
          <Pressable
            accessibilityRole="link"
            onPress={() =>
              void Linking.openURL("https://console.x.com/").catch(() =>
                setMessage("暂时无法打开控制台，请访问 console.x.com。"),
              )
            }
            style={{ paddingTop: 10, paddingBottom: 4 }}
          >
            <Text style={{ color: C.green, fontSize: 12, fontWeight: "600" }}>
              打开 X 开发者控制台 ↗
            </Text>
          </Pressable>
        </View>
      )}
      {!!freshness?.overdue_accounts && !xPaymentRequired && (
        <Text style={{ fontSize: 11, color: C.accent, marginTop: 5 }}>
          {freshness.overdue_accounts}{" "}
          个关注账号尚未达到时效目标，最新窗口仍有延迟。
        </Text>
      )}
      {!!message && (
        <Text
          accessibilityLiveRegion="polite"
          style={[s.muted, { fontSize: 11, marginTop: 6 }]}
        >
          {message}
        </Text>
      )}
      <Modal
        visible={open}
        animationType="slide"
        presentationStyle="pageSheet"
        onRequestClose={() => setOpen(false)}
      >
        <SafeAreaView style={[s.screen, { flex: 1 }]}>
          <View style={[s.spread, { padding: 24 }]}>
            <View>
              <Text style={s.sectionTitle}>翻译归档</Text>
              <Text style={[s.muted, { marginTop: 5 }]}>
                按中文完成时间排列，原文始终保留。
              </Text>
            </View>
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="关闭翻译归档"
              onPress={() => setOpen(false)}
            >
              <Feather name="x" size={22} color={C.ink} />
            </Pressable>
          </View>
          <ScrollView
            contentContainerStyle={{ paddingHorizontal: 24, paddingBottom: 32 }}
          >
            {updates.data?.offline && (
              <Text style={s.muted}>当前显示离线归档。</Text>
            )}
            {updates.error && !items.length && (
              <Text style={s.muted}>暂时无法获取翻译归档，请稍后重试。</Text>
            )}
            {!updates.error && !items.length && (
              <Text style={s.muted}>
                {updates.isPending && !connection.demo
                  ? "正在读取归档…"
                  : "完成的中文版本会自动出现在这里。"}
              </Text>
            )}
            {items.map((item) => (
              <Pressable
                key={item.id}
                accessibilityRole="button"
                onPress={() => {
                  setOpen(false);
                  onOpen(item.article_id);
                }}
                style={{
                  paddingVertical: 18,
                  borderBottomWidth: 1,
                  borderColor: C.line,
                }}
              >
                <Text style={[s.muted, { fontSize: 11 }]}>
                  {item.author} ·{" "}
                  {item.kind === "web_translation" ? "关联网页" : "发言"}
                  中文已完成
                </Text>
                <Text style={[s.body, { marginTop: 7, fontWeight: "600" }]}>
                  {item.title_zh || item.title}
                </Text>
                <Text style={[s.muted, { marginTop: 7, fontSize: 11 }]}>
                  {new Date(item.completed_at).toLocaleString()}
                </Text>
              </Pressable>
            ))}
            {items.length >= 200 && (
              <Text style={[s.muted, { marginTop: 12 }]}>
                显示最近 200 条完成记录，完整文章仍可在雷达搜索。
              </Text>
            )}
          </ScrollView>
        </SafeAreaView>
      </Modal>
    </View>
  );
}
