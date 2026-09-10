import React, { useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { C, s } from "./theme";
import type { Connection } from "./types";
import {
  accountBadge,
  money,
  windowName,
  type AccountReport,
} from "./accountView";

const stamp = (value: string) =>
  new Date(value).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

export default function AccountsPanel({
  connection,
}: {
  connection: Connection;
}) {
  const [expanded, setExpanded] = useState(false);
  const client = useQueryClient();
  const key = ["accounts", connection.url, connection.token];
  const query = useQuery({
    queryKey: key,
    // Financial data remains in memory; don't put it in the shared offline article cache.
    queryFn: ({ signal }) =>
      api<AccountReport>(connection, "/v1/accounts", { signal }),
    refetchInterval: (q) => (q.state.data?.refreshing ? 3000 : 30000),
    retry: 1,
  });
  const refresh = useMutation({
    mutationFn: () =>
      api<AccountReport>(connection, "/v1/accounts/refresh", {
        method: "POST",
      }),
    onSuccess: (value) => client.setQueryData(key, value),
  });
  const data = query.data;
  const updating = refresh.isPending || data?.refreshing;
  return (
    <View style={{ marginBottom: 28 }}>
      <View style={s.spread}>
        <Text style={a.heading}>余额与额度</Text>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="刷新账户余额与额度"
          disabled={!!updating}
          onPress={() => refresh.mutate()}
          style={a.refresh}
        >
          <Text style={{ color: C.green }}>
            {updating ? "正在更新…" : "刷新"}
          </Text>
        </Pressable>
      </View>
      <Text style={[s.muted, { lineHeight: 21, marginBottom: 14 }]}>
        厂商账户共享，可能包含你在其他应用中的使用。服务器每{" "}
        {Math.round((data?.refresh_seconds || 300) / 60)} 分钟更新。
      </Text>
      {query.isPending && <ActivityIndicator color={C.green} />}
      {(query.isError || refresh.isError) && (
        <Text style={a.warning}>暂时无法更新账户信息，已有数值仅供参考。</Text>
      )}
      {data && !data.enabled && (
        <Text style={s.muted}>服务端尚未启用账户查询。</Text>
      )}
      {data?.items.map((account) => {
        const badge = accountBadge(account, query.isError);
        const warning = [
          "low",
          "exhausted",
          "authorization_required",
          "query_failed",
          "invalid_response",
        ].includes(account.status);
        const stale = account.stale || query.isError;
        const limits = expanded ? account.limits : account.limits.slice(0, 1);
        return (
          <View key={account.id} style={a.card}>
            <View style={s.spread}>
              <Text style={a.title}>{account.name}</Text>
              <Text style={[a.badge, { color: warning ? C.accent : C.muted }]}>
                {badge}
              </Text>
            </View>
            {!account.in_use && (
              <Text style={[s.muted, { marginTop: 4 }]}>
                备用账户 · 当前未用于内容处理
              </Text>
            )}
            {account.balances.map((b) => (
              <View key={b.currency} style={{ marginTop: 15 }}>
                <Text style={s.muted}>
                  {stale ? "上次查询的可用余额" : "可用余额"} · {b.currency}
                </Text>
                <Text selectable style={a.amount}>
                  {money(b.amount, b.currency)}
                </Text>
                <Text style={s.muted}>
                  现金 {money(b.cash_amount, b.currency)}
                  {b.granted_amount != null
                    ? ` · 赠金 ${money(b.granted_amount, b.currency)}`
                    : ""}
                </Text>
              </View>
            ))}
            {limits.map((bucket) => (
              <View key={bucket.id} style={{ marginTop: 16 }}>
                <Text style={[s.body, { fontWeight: "600", marginBottom: 8 }]}>
                  {bucket.id === "codex" ? "Codex" : bucket.name}
                  {account.plan ? ` · ${account.plan.toUpperCase()}` : ""}
                </Text>
                {bucket.windows.map((w) => {
                  const expired =
                    w.expired || Date.parse(w.resets_at) <= Date.now();
                  return (
                    <View key={w.name} style={{ marginBottom: 12 }}>
                      <View style={s.spread}>
                        <Text style={s.muted}>
                          {windowName(w.window_minutes)}
                        </Text>
                        <Text style={s.body}>
                          {expired
                            ? "已到重置时间"
                            : `${stale ? "上次" : ""}剩余 ${Number(w.remaining_percent.toFixed(1))}%`}
                        </Text>
                      </View>
                      <View style={a.track}>
                        <View
                          style={[
                            a.fill,
                            {
                              width: `${expired ? 0 : Math.max(0, Math.min(100, w.remaining_percent))}%`,
                              backgroundColor:
                                stale || expired
                                  ? C.muted
                                  : w.remaining_percent <= 10
                                    ? C.accent
                                    : C.green,
                            },
                          ]}
                        />
                      </View>
                      <Text style={[s.muted, { fontSize: 12 }]}>
                        {expired
                          ? "等待服务器确认新额度"
                          : `${stamp(w.resets_at)} 重置`}
                      </Text>
                    </View>
                  );
                })}
                {bucket.credits && (
                  <Text style={[s.body, { marginTop: 2 }]}>
                    {stale ? "上次 " : ""}Credits：
                    {bucket.credits.unlimited
                      ? "不限额"
                      : bucket.credits.balance == null
                        ? "未报告"
                        : Number(bucket.credits.balance).toLocaleString(
                            "zh-CN",
                            { maximumFractionDigits: 2 },
                          )}
                  </Text>
                )}
              </View>
            ))}
            {account.limits.length > 1 && (
              <Pressable
                accessibilityRole="button"
                onPress={() => setExpanded(!expanded)}
                style={a.refresh}
              >
                <Text style={{ color: C.green }}>
                  {expanded
                    ? "收起其他额度"
                    : `其他额度 · ${account.limits.length - 1} 项`}
                </Text>
              </Pressable>
            )}
            {!!account.message && (
              <Text style={a.warning}>
                {account.provider === "bailian" &&
                account.status === "authorization_required"
                  ? "需要阿里云财务只读授权；模型调用密钥不能查询账户余额。"
                  : account.message}
              </Text>
            )}
            {!!account.scope && (
              <Text
                style={[
                  s.muted,
                  { marginTop: 12, fontSize: 12, lineHeight: 19 },
                ]}
              >
                {account.scope}
              </Text>
            )}
            {!!account.last_success_at && (
              <Text style={[s.muted, { marginTop: 8, fontSize: 12 }]}>
                {stale ? "上次成功查询" : "查询于"}{" "}
                {stamp(account.last_success_at)}
              </Text>
            )}
          </View>
        );
      })}
    </View>
  );
}

const a = StyleSheet.create({
  heading: { color: C.ink, fontSize: 20, fontWeight: "700" },
  refresh: {
    minHeight: 44,
    justifyContent: "center",
    alignSelf: "flex-start",
    paddingHorizontal: 8,
  },
  card: {
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 16,
    padding: 18,
    backgroundColor: C.paper,
    marginBottom: 12,
  },
  title: { color: C.ink, fontSize: 16, fontWeight: "700", flex: 1 },
  badge: { fontSize: 11, marginLeft: 8 },
  amount: {
    color: C.ink,
    fontSize: 32,
    fontWeight: "600",
    marginVertical: 8,
    fontVariant: ["tabular-nums"],
  },
  warning: { color: C.accent, fontSize: 13, lineHeight: 21, marginTop: 10 },
  track: {
    height: 6,
    backgroundColor: C.line,
    borderRadius: 3,
    overflow: "hidden",
    marginTop: 9,
    marginBottom: 7,
  },
  fill: { height: 6, borderRadius: 3 },
});
