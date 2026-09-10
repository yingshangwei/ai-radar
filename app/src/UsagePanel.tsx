import React, { useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useQuery } from "@tanstack/react-query";
import { cached } from "./api";
import { C, s } from "./theme";
import type { Connection } from "./types";
import {
  modelName,
  modelTotals,
  providerName,
  tokenAmount,
  type UsageReport,
} from "./usageView";

const PERIODS = [
  ["today", "今天"],
  ["7d", "7 天"],
  ["30d", "30 天"],
  ["all", "累计"],
] as const;
const stamp = (value: string) =>
  new Date(value).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

export default function UsagePanel({ connection }: { connection: Connection }) {
  const [period, setPeriod] = useState<string>("today");
  const [expanded, setExpanded] = useState(false);
  const query = useQuery({
    queryKey: ["usage", connection.url, connection.token, period],
    queryFn: ({ signal }) =>
      cached<UsageReport>(connection, `/v1/usage?period=${period}`, signal),
    refetchInterval: 60000,
  });
  const data = query.data?.data;
  const groups = data?.groups || [];
  const feature = (value: string) => data?.features[value] || value;
  const stage = (value: string) => data?.stages[value] || value;
  return (
    <View>
      <Text style={[s.muted, { marginBottom: 18 }]}>
        每一份前沿信息，模型用了多少 Token。
      </Text>
      <View style={u.tabs}>
        {PERIODS.map(([id, title]) => (
          <Pressable
            key={id}
            accessibilityRole="tab"
            accessibilityState={{ selected: period === id }}
            onPress={() => setPeriod(id)}
            style={[u.tab, period === id && u.selected]}
          >
            <Text
              style={{
                color: period === id ? C.paper : C.muted,
                fontWeight: "600",
              }}
            >
              {title}
            </Text>
          </Pressable>
        ))}
      </View>
      {query.isPending && (
        <ActivityIndicator color={C.green} style={{ marginVertical: 24 }} />
      )}
      {query.isError && <Text style={u.warning}>{query.error.message}</Text>}
      {data && !data.available && (
        <Text style={u.warning}>
          {data.error || "服务端尚未启用用量统计。"}
        </Text>
      )}
      {data?.available && (
        <>
          <View style={u.hero}>
            <Text style={u.heroLabel}>
              {query.data?.offline ? "离线缓存 · " : ""}已报告消耗
            </Text>
            <View style={[s.row, { gap: 9, alignItems: "baseline" }]}>
              <Text
                accessibilityLabel={`已报告 ${data.totals.total_tokens ?? "未知"} tokens`}
                style={u.number}
              >
                {tokenAmount(data.totals.total_tokens)}
              </Text>
              <Text style={u.heroLabel}>TOKENS</Text>
            </View>
            <View style={[s.spread, { marginTop: 18 }]}>
              <Text style={u.heroDetail}>
                输入 {tokenAmount(data.totals.input_tokens)}
              </Text>
              <Text style={u.heroDetail}>
                输出 {tokenAmount(data.totals.output_tokens)}
              </Text>
            </View>
          </View>
          <Text style={[s.muted, { marginTop: 12, lineHeight: 21 }]}>
            {data.totals.reported_calls} 次有用量 · {data.totals.active_calls}{" "}
            次进行中 · {data.totals.unknown_calls} 次用量未知
          </Text>
          {(data.totals.unknown_calls > 0 || data.recording_errors > 0) && (
            <Text style={u.warning}>
              部分调用未取得用量回执，实际消耗可能更高。未知用量不会按零计入。
              {data.recording_errors > 0
                ? "统计写入曾发生异常，需检查服务端。"
                : ""}
            </Text>
          )}
          <View style={[s.note, { marginTop: 16 }]}>
            <Text style={s.body}>
              缓存命中 {tokenAmount(data.totals.cached_tokens)} · 推理{" "}
              {tokenAmount(data.totals.reasoning_tokens)}
            </Text>
            <Text style={[s.muted, { marginTop: 5, lineHeight: 20 }]}>
              分别包含在输入、输出中，不额外相加。未报告的项显示 —。
            </Text>
          </View>
          <Text style={u.heading}>按模型</Text>
          {modelTotals(groups).map((row) => (
            <View key={`${row.provider}/${row.model}`} style={u.modelRow}>
              <View style={{ flex: 1, paddingRight: 10 }}>
                <Text style={u.model}>{modelName(row.model)}</Text>
                <Text style={s.muted}>
                  {providerName(row.provider)} · {row.calls} 次调用
                </Text>
              </View>
              <Text style={u.amount}>{tokenAmount(row.tokens)}</Text>
            </View>
          ))}
          {!groups.length && (
            <Text style={s.muted}>这个时段还没有模型调用记录。</Text>
          )}
          <Text style={u.heading}>功能消耗明细</Text>
          {groups.map((row) => (
            <View
              key={[row.provider, row.model, row.feature, row.stage].join("/")}
              style={u.detail}
            >
              <View style={s.spread}>
                <Text style={[s.body, { fontWeight: "600", flex: 1 }]}>
                  {feature(row.feature)}
                </Text>
                <Text style={u.amount}>{tokenAmount(row.total_tokens)}</Text>
              </View>
              <Text style={[s.muted, { marginTop: 5 }]}>
                {stage(row.stage)} · {modelName(row.model)}
              </Text>
              <Text style={[s.muted, { marginTop: 9, lineHeight: 20 }]}>
                输入 {tokenAmount(row.input_tokens)} / 输出{" "}
                {tokenAmount(row.output_tokens)}
                {"\n"}缓存 {tokenAmount(row.cached_tokens)} / 推理{" "}
                {tokenAmount(row.reasoning_tokens)} · {row.calls} 次
                {row.unknown_calls > 0
                  ? ` · ${row.unknown_calls} 次用量未知`
                  : ""}
              </Text>
            </View>
          ))}
          <Pressable
            onPress={() => setExpanded(!expanded)}
            style={u.link}
            accessibilityRole="button"
          >
            <Text style={{ color: C.green, fontWeight: "600" }}>
              {expanded ? "收起" : "展开"}最近调用 · 最多 50 条
            </Text>
          </Pressable>
          {expanded &&
            data.recent.map((row) => (
              <View key={row.id} style={u.detail}>
                <View style={s.spread}>
                  <Text style={s.muted}>{stamp(row.started_at)}</Text>
                  <Text style={u.amount}>{tokenAmount(row.total_tokens)}</Text>
                </View>
                <Text style={[s.body, { marginTop: 7 }]}>
                  {feature(row.feature)} · {stage(row.stage)}
                </Text>
                <Text style={[s.muted, { marginTop: 4 }]}>
                  {modelName(row.model)} ·{" "}
                  {row.outcome === "started"
                    ? "进行中"
                    : row.total_tokens == null
                      ? "用量未知"
                      : row.outcome === "error"
                        ? "调用失败 · 已计用量"
                        : "已取得回执"}
                </Text>
                <Text
                  selectable
                  style={[s.muted, { fontSize: 11, marginTop: 5 }]}
                >
                  输入 {row.input_tokens?.toLocaleString() ?? "—"} / 输出{" "}
                  {row.output_tokens?.toLocaleString() ?? "—"} tokens
                </Text>
              </View>
            ))}
          <Text style={[s.muted, { lineHeight: 21, marginTop: 16 }]}>
            从 {stamp(data.tracking_started_at)}{" "}
            开始记录；不估算此前未保存的消耗。 按北京时间分日。K = 千，M =
            百万，B = 十亿。Token 用量不等同于账单金额。
          </Text>
          <Pressable
            onPress={() => query.refetch()}
            style={u.link}
            disabled={query.isFetching}
          >
            <Text style={{ color: C.green }}>
              {query.isFetching ? "正在更新…" : "刷新用量"} · 更新于{" "}
              {stamp(data.as_of)}
            </Text>
          </Pressable>
        </>
      )}
      {!!data?.allocation?.length && (
        <>
          <Text style={u.heading}>当前模型分工</Text>
          <Text style={[s.muted, { marginBottom: 12 }]}>
            这是当前配置；上方明细保留每次调用当时的模型。
          </Text>
          {Object.entries(data.features)
            .filter(([key]) =>
              data.allocation.some((row) => row.feature === key),
            )
            .map(([key, title]) => (
              <View key={key} style={u.detail}>
                <Text style={[s.body, { fontWeight: "600", marginBottom: 5 }]}>
                  {title}
                </Text>
                {data.allocation
                  .filter((row) => row.feature === key)
                  .map((row) => (
                    <Text key={row.stage} style={[s.muted, { lineHeight: 23 }]}>
                      {stage(row.stage)}　{modelName(row.model)} ·{" "}
                      {providerName(row.provider)}
                    </Text>
                  ))}
              </View>
            ))}
        </>
      )}
    </View>
  );
}

const u = StyleSheet.create({
  tabs: {
    flexDirection: "row",
    backgroundColor: C.line,
    borderRadius: 12,
    padding: 4,
    marginBottom: 20,
  },
  tab: { flex: 1, paddingVertical: 11, alignItems: "center", borderRadius: 9 },
  selected: { backgroundColor: C.dark },
  hero: { backgroundColor: C.dark, borderRadius: 18, padding: 24 },
  heroLabel: { color: "#B9C7BA", fontSize: 12, letterSpacing: 1 },
  heroDetail: { color: "#E1E9DC", fontSize: 14 },
  number: {
    color: C.paper,
    fontSize: 48,
    lineHeight: 65,
    fontWeight: "600",
    fontVariant: ["tabular-nums"],
  },
  warning: { color: C.accent, lineHeight: 22, marginVertical: 12 },
  heading: {
    color: C.ink,
    fontSize: 19,
    fontWeight: "700",
    marginTop: 30,
    marginBottom: 14,
  },
  modelRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingVertical: 15,
    borderBottomColor: C.line,
    borderBottomWidth: 1,
  },
  model: { color: C.ink, fontSize: 15, fontWeight: "600", marginBottom: 5 },
  amount: {
    color: C.ink,
    fontSize: 18,
    fontWeight: "600",
    fontVariant: ["tabular-nums"],
  },
  detail: {
    backgroundColor: C.paper,
    padding: 17,
    borderColor: C.line,
    borderWidth: 1,
    borderRadius: 12,
    marginBottom: 10,
  },
  link: { minHeight: 48, justifyContent: "center", marginVertical: 10 },
});
