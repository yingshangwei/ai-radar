import React, { useMemo, useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import Svg, {
  Circle,
  Line,
  Polyline,
  Rect,
  Text as SvgText,
} from "react-native-svg";
import { useMarket } from "./useMarket";
import { C, s } from "./theme";
import type { Connection } from "./types";
import type { MarketChart } from "./marketTypes";
import { marketReading } from "./marketView";

const colors = ["#0072B2", "#D55E00", "#242424", "#8B4BA8", "#00815F"];
const dashes = [undefined, "7 4", "2 4", "10 3 2 3", "4 3"];
const money = (v: number | undefined, digits = 4) =>
  v == null || !Number.isFinite(v)
    ? "—"
    : v.toLocaleString("zh-CN", {
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      });
const stamp = (v?: number) =>
  v
    ? new Date(v * 1000).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "尚无数据";
const shortTime = (v: number) =>
  new Date(v * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
function Choice({
  label,
  selected,
  onPress,
}: {
  label: string;
  selected?: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected }}
      onPress={onPress}
      style={[m.choice, selected && m.chosen]}
    >
      <Text style={[m.choiceText, selected && { color: C.paper }]}>
        {label}
      </Text>
    </Pressable>
  );
}
function Choices({
  label,
  values,
  current,
  change,
}: {
  label: string;
  values: [string, string][];
  current: string;
  change: (v: string) => void;
}) {
  return (
    <View style={{ marginTop: 14 }}>
      <Text style={m.smallLabel}>{label}</Text>
      <View style={m.wrap}>
        {values.map(([value, title]) => (
          <Choice
            key={value}
            label={title}
            selected={current === value}
            onPress={() => change(value)}
          />
        ))}
      </View>
    </View>
  );
}
const Trend = React.memo(function Trend({
  title,
  unit,
  chart,
}: {
  title: string;
  unit: string;
  chart?: MarketChart;
}) {
  const [hidden, setHidden] = useState<string[]>([]),
    [cursor, setCursor] = useState<number | null>(null),
    [plotWidth, setPlotWidth] = useState(330);
  const series = chart?.series ?? [],
    visible = series.filter((x) => !hidden.includes(x.name));
  const values = visible
    .flatMap((x) => x.strokes.flatMap((y) => y.map((p) => p.value)))
    .filter(Number.isFinite);
  const lo = values.length ? Math.min(...values) : 0,
    hi = values.length ? Math.max(...values) : 1,
    pad = Math.max((hi - lo) * 0.12, Math.abs(hi) * 0.00005, 0.0001);
  const low = lo - pad,
    high = hi + pad,
    w = 330,
    h = 180,
    left = 54,
    right = 12,
    top = 14,
    bottom = 28;
  const start = chart?.range.start ?? 0,
    end = chart?.range.end ?? 1;
  const x = (v: number) =>
      left + ((v - start) / Math.max(1, end - start)) * (w - left - right),
    y = (v: number) => top + ((high - v) / (high - low)) * (h - top - bottom);
  const at = cursor == null ? null : start + cursor * (end - start);
  return (
    <View style={m.card}>
      <View style={s.spread}>
        <Text style={m.cardTitle}>{title}</Text>
        <Text style={s.muted}>{unit}</Text>
      </View>
      {!values.length ? (
        <Text style={[s.muted, { paddingVertical: 30 }]}>
          当前条件下暂无有效样本；缺失数据不会补成零。
        </Text>
      ) : (
        <View
          onLayout={(e) => setPlotWidth(e.nativeEvent.layout.width)}
          onStartShouldSetResponder={() => true}
          onResponderGrant={(e) =>
            setCursor(
              Math.max(
                0,
                Math.min(
                  1,
                  ((e.nativeEvent.locationX / Math.max(1, plotWidth)) * w -
                    left) /
                    (w - left - right),
                ),
              ),
            )
          }
        >
          <Svg
            pointerEvents="none"
            viewBox={`0 0 ${w} ${h}`}
            width="100%"
            height={185}
            accessibilityLabel={`${title}趋势图`}
          >
            {[0, 0.5, 1].map((t) => (
              <React.Fragment key={t}>
                <Line
                  x1={left}
                  x2={w - right}
                  y1={top + t * (h - top - bottom)}
                  y2={top + t * (h - top - bottom)}
                  stroke={C.line}
                  strokeDasharray="3 4"
                />
                <SvgText
                  x={left - 7}
                  y={top + t * (h - top - bottom) + 4}
                  textAnchor="end"
                  fontSize={9}
                  fill={C.muted}
                >
                  {money(high - t * (high - low), Math.abs(high) < 20 ? 3 : 1)}
                </SvgText>
              </React.Fragment>
            ))}
            {series.map((item, index) =>
              hidden.includes(item.name)
                ? null
                : item.strokes.map((points, k) => (
                    <React.Fragment key={`${item.name}-${k}`}>
                      {points.length > 1 && (
                        <Polyline
                          points={points
                            .map((p) => `${x(p.time)},${y(p.value)}`)
                            .join(" ")}
                          fill="none"
                          stroke={colors[index % 5]}
                          strokeWidth={1.8}
                          strokeDasharray={dashes[index % 5]}
                        />
                      )}
                      {points
                        .filter(
                          (_, i) =>
                            i === 0 ||
                            i === points.length - 1 ||
                            i % Math.max(1, Math.floor(points.length / 5)) ===
                              0,
                        )
                        .map((p, i) =>
                          index % 2 ? (
                            <Rect
                              key={i}
                              x={x(p.time) - 2}
                              y={y(p.value) - 2}
                              width={4}
                              height={4}
                              fill={colors[index % 5]}
                            />
                          ) : (
                            <Circle
                              key={i}
                              cx={x(p.time)}
                              cy={y(p.value)}
                              r={2}
                              fill={colors[index % 5]}
                            />
                          ),
                        )}
                    </React.Fragment>
                  )),
            )}
            {at != null && (
              <Line
                x1={x(at)}
                x2={x(at)}
                y1={top}
                y2={h - bottom}
                stroke={C.muted}
              />
            )}
            <SvgText x={left} y={h - 6} fontSize={9} fill={C.muted}>
              {stamp(start)}
            </SvgText>
            <SvgText
              x={w - right}
              y={h - 6}
              fontSize={9}
              textAnchor="end"
              fill={C.muted}
            >
              {stamp(end)}
            </SvgText>
          </Svg>
        </View>
      )}
      <View style={m.wrap}>
        {series.map((item, index) => (
          <Pressable
            accessibilityRole="button"
            key={item.name}
            onPress={() =>
              setHidden(
                hidden.includes(item.name)
                  ? hidden.filter((x) => x !== item.name)
                  : [...hidden, item.name],
              )
            }
            style={{
              flexDirection: "row",
              alignItems: "center",
              gap: 5,
              opacity: hidden.includes(item.name) ? 0.3 : 1,
              paddingVertical: 5,
            }}
          >
            <View
              style={{
                width: 15,
                height: 3,
                backgroundColor: colors[index % 5],
              }}
            />
            <Text style={[s.muted, { fontSize: 10 }]}>
              {item.name}
              {at != null
                ? (() => {
                    const point = marketReading(item, at);
                    return point
                      ? ` ${money(point.value)} · ${shortTime(point.time)}`
                      : " — 此处缺采样";
                  })()
                : ""}
            </Text>
          </Pressable>
        ))}
      </View>
      <Text style={[s.muted, { fontSize: 10 }]}>
        {at == null
          ? "点击图例显隐曲线 · 同价曲线可能重合"
          : `所选时间 ${shortTime(at)} · 数值后为实际绘图采样时间`}
      </Text>
      {at != null && (
        <Pressable accessibilityRole="button" onPress={() => setCursor(null)}>
          <Text style={[s.muted, { fontSize: 11, paddingTop: 8 }]}>
            取消读数
          </Text>
        </Pressable>
      )}
    </View>
  );
});
export default function MarketPanel({
  connection,
  active = true,
}: {
  connection: Connection;
  active?: boolean;
}) {
  const [payment, setPayment] = useState("merged"),
    [ticket, setTicket] = useState("10000"),
    [mode, setMode] = useState("strict"),
    [hours, setHours] = useState("24"),
    [interval, setInterval] = useState("5m"),
    [smoothing, setSmoothing] = useState("300"),
    [end, setEnd] = useState<number | undefined>(),
    [timeText, setTimeText] = useState(""),
    [timeError, setTimeError] = useState("");
  const params = useMemo(
    () =>
      new URLSearchParams({
        payment,
        ticket,
        mode,
        hours,
        interval,
        smoothing,
        ...(end ? { end: String(end) } : {}),
      }).toString(),
    [payment, ticket, mode, hours, interval, smoothing, end],
  );
  const query = useMarket(connection, params, active);
  const data = query.data,
    latest = data?.dashboard.latestCycle;
  const age = latest ? Date.now() / 1000 - latest.endedAt : Infinity;
  const healthy = Boolean(
    query.verified &&
    !query.refreshing &&
    data?.dashboard.service.running &&
    latest &&
    age < 180 &&
    latest.failed === 0,
  );
  return (
    <ScrollView
      contentContainerStyle={{ paddingHorizontal: 20, paddingBottom: 30 }}
      refreshControl={
        <RefreshControl
          refreshing={query.isRefetching}
          onRefresh={() => void query.refetch()}
        />
      }
    >
      <View style={s.section}>
        <Text style={s.label}>TIDEWATCH · 云端行情</Text>
        <Text style={[s.h1, { marginTop: 8 }]}>水位，持续观察。</Text>
        <Text style={[s.muted, { marginTop: 7 }]}>
          USDT / CNY · C2C 与现货对照
        </Text>
      </View>
      <View style={m.hero}>
        <View style={s.spread}>
          <Text style={m.heroLabel}>
            {data && !query.verified
              ? "◷ 本地缓存 · 等待联网更新"
              : query.refreshing
                ? "◷ 已保存快照 · 后台更新中"
                : healthy
                  ? "● 云端持续采集"
                  : data
                    ? "◐ 请关注采集状态"
                    : "○ 正在连接行情服务"}
          </Text>
          <Pressable
            accessibilityRole="button"
            onPress={() => void query.refetch()}
          >
            <Text style={m.heroLabel}>刷新 ↻</Text>
          </Pressable>
        </View>
        <Text style={m.heroTitle}>电脑休息，采样继续。</Text>
        <Text style={m.heroNote}>
          {data
            ? `最近一轮 ${stamp(latest?.endedAt)} · ${data.dashboard.config.intervalSeconds} 秒采样`
            : "连接后显示服务器已保存的数据"}
        </Text>
      </View>
      {data && (
        <Text style={[s.muted, { marginTop: 10, fontSize: 11 }]}>
          视图更新 {stamp(data.generatedAt)} · 每 60 秒检查变化
          {!query.verified ? " · 当前展示缓存数据" : ""}
        </Text>
      )}
      {query.isPending && (
        <ActivityIndicator style={{ margin: 24 }} color={C.accent} />
      )}
      {query.error && (
        <View style={m.notice}>
          <Text style={s.body}>{query.error.message}</Text>
        </View>
      )}
      <Choices
        label="付款渠道"
        values={[
          ["merged", "合并"],
          ["bank", "银行卡"],
          ["alipay", "支付宝"],
        ]}
        current={payment}
        change={setPayment}
      />
      <Choices
        label="单笔人民币金额"
        values={[
          ["5000", "¥5,000"],
          ["10000", "¥10,000"],
          ["50000", "¥50,000"],
        ]}
        current={ticket}
        change={setTicket}
      />
      <Choices
        label="商家资格"
        values={[
          ["strict", "完整质量"],
          ["rateOnly", "历史对照"],
        ]}
        current={mode}
        change={setMode}
      />
      <Text style={[s.muted, { marginTop: 6 }]}>
        {mode === "strict"
          ? "认证/钻石商家 · ≥1,000 单 · 完成率 ≥98%。缺少质量字段的旧样本不计入。"
          : "仅按已保存的完成率筛选，供旧历史对照；不补造商家等级。"}
      </Text>
      <View style={m.card}>
        <Text style={m.cardTitle}>五档加权均价</Text>
        <Text style={[s.muted, { marginTop: 4 }]}>
          以下为最新观测，方向按你的买入 / 卖出 USDT。
        </Text>
        <View style={[m.tableRow, { marginTop: 12 }]}>
          <Text style={m.tableHead}>累计金额</Text>
          <Text style={m.tableHead}>买入</Text>
          <Text style={m.tableHead}>卖出</Text>
        </View>
        {[10000, 100000, 300000, 500000, 1000000].map((amount) => (
          <View key={amount} style={m.tableRow}>
            <Text style={m.cell}>{amount / 10000} 万</Text>
            {["buy", "sell"].map((side) => {
              const row = data?.waterlines[side],
                tier = row?.tiers.find((t) => t.thresholdCNY === amount);
              return (
                <View key={side} style={{ flex: 1 }}>
                  <Text
                    style={[
                      m.price,
                      { color: side === "buy" ? colors[0] : colors[1] },
                    ]}
                  >
                    {money(tier?.average)}
                  </Text>
                  <Text style={m.cellHint}>
                    {tier
                      ? `${tier.merchantCount} 商家 · ${Math.min(100, (Number(tier.filledCNY) / amount) * 100).toFixed(0)}% 覆盖`
                      : "等待样本"}
                  </Text>
                </View>
              );
            })}
          </View>
        ))}
        <Text style={[s.muted, { marginTop: 10 }]}>
          买 {stamp(data?.waterlines.buy?.observedAt)} · 卖{" "}
          {stamp(data?.waterlines.sell?.observedAt)}
        </Text>
      </View>
      <Choices
        label="所有趋势图共用时间范围"
        values={[
          ["6", "6 小时"],
          ["24", "24 小时"],
          ["72", "3 天"],
          ["168", "7 天"],
        ]}
        current={hours}
        change={setHours}
      />
      <Choices
        label="聚合周期"
        values={[
          ["1m", "1 分"],
          ["5m", "5 分"],
          ["15m", "15 分"],
          ["1h", "1 小时"],
          ["1d", "1 天"],
          ["7d", "7 天"],
        ]}
        current={interval}
        change={setInterval}
      />
      <Choices
        label="时间平滑"
        values={[
          ["0", "原始"],
          ["300", "5 分 EMA"],
          ["900", "15 分 EMA"],
        ]}
        current={smoothing}
        change={setSmoothing}
      />
      <View style={[m.wrap, { marginTop: 12 }]}>
        <TextInput
          accessibilityLabel="历史结束时间"
          placeholder="结束时间，如 2026-09-14T12:00+08:00"
          value={timeText}
          onChangeText={setTimeText}
          style={m.timeInput}
        />
        <Choice
          label="查看历史"
          onPress={() => {
            const t = Date.parse(timeText) / 1000;
            if (
              !Number.isFinite(t) ||
              t > Date.now() / 1000 ||
              t < Date.now() / 1000 - 30 * 86400
            ) {
              setTimeError("请输入带时区的有效日期，范围为最近 30 天。");
              return;
            }
            setTimeError("");
            setEnd(t);
          }}
        />
        <Choice
          label="返回实时"
          selected={!end}
          onPress={() => {
            setEnd(undefined);
            setTimeError("");
          }}
        />
      </View>
      {!!timeError && (
        <Text style={{ color: C.accent, marginTop: 6 }}>{timeError}</Text>
      )}
      <Text style={[s.muted, { marginVertical: 12 }]}>
        {end ? `固定结束于 ${stamp(end)}` : "跟随最新时间"} ·
        聚合使用观测均值，容量不跨时间累加。缺口断线，样本不足一档时显示单点。
      </Text>
      <Trend
        title="买入 USDT 水位"
        unit="CNY / USDT"
        chart={data?.charts["waterline/buy"]}
      />
      <Trend
        title="卖出 USDT 水位"
        unit="CNY / USDT"
        chart={data?.charts["waterline/sell"]}
      />
      <Trend
        title="现货涨跌对照"
        unit="%"
        chart={data?.charts["spot/normalized"]}
      />
      <Trend title="固定价带容量" unit="万元" chart={data?.charts.capacity} />
      <View style={m.card}>
        <Text style={m.cardTitle}>现货快照</Text>
        {data?.dashboard.tickers.map((t) => (
          <View style={m.tableRow} key={t.pair}>
            <Text style={m.cell}>{t.pair.replace("-USDT", "")}</Text>
            <Text style={m.price}>{money(t.last, 2)}</Text>
            <Text style={m.cell}>
              {t.open24h > 0
                ? `${((t.last / t.open24h - 1) * 100).toFixed(2)}%`
                : "—"}
            </Text>
          </View>
        ))}
        <Text style={[s.muted, { marginTop: 10 }]}>
          压力观察：
          {data?.pressure.score != null
            ? data.pressure.score.toFixed(1)
            : "尚不具备评分条件"}
          。需充分的历史与商家分散度，缺值不代表中性。
        </Text>
      </View>
      <View style={m.card}>
        <Text style={m.cardTitle}>采集与数据范围</Text>
        {data?.dashboard.streams
          .filter((x) => x.error || x.blocked)
          .map((x) => (
            <Text
              key={x.id}
              style={[s.muted, { marginTop: 6, color: C.accent }]}
            >
              {x.id} · {x.error || "访问受限"}
            </Text>
          ))}
        <Text style={[s.muted, { marginTop: 10 }]}>
          {data
            ? `${(data.dashboard.counts.cycles ?? 0).toLocaleString()} 轮采样 · ${(data.dashboard.counts.candles ?? 0).toLocaleString()} 根历史 K 线\n服务器原始快照保留 ${data.storage.rawDays} 天，指标保留 ${data.storage.metricDays} 天。\n`
            : ""}
          公开广告是有限样本，不代表成交量或保证可成交的容量。同步不会补造历史缺口。
        </Text>
      </View>
    </ScrollView>
  );
}
const m = StyleSheet.create({
  hero: { backgroundColor: C.dark, borderRadius: 20, padding: 20 },
  heroLabel: { color: "#DBE5D7", fontSize: 11 },
  heroTitle: { color: C.paper, fontSize: 24, fontWeight: "600", marginTop: 20 },
  heroNote: { color: "#BFCBB9", fontSize: 11, marginTop: 12, lineHeight: 19 },
  wrap: { flexDirection: "row", flexWrap: "wrap", gap: 7, marginTop: 6 },
  choice: {
    borderRadius: 10,
    borderWidth: 1,
    borderColor: C.line,
    paddingHorizontal: 11,
    paddingVertical: 9,
    backgroundColor: C.paper,
  },
  chosen: { backgroundColor: C.dark, borderColor: C.dark },
  choiceText: { fontSize: 12, color: C.ink },
  smallLabel: { color: C.muted, fontSize: 11, fontWeight: "600" },
  card: {
    backgroundColor: C.paper,
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 17,
    padding: 16,
    marginTop: 18,
  },
  cardTitle: { fontSize: 17, fontWeight: "700", color: C.ink },
  tableRow: {
    flexDirection: "row",
    alignItems: "center",
    borderBottomWidth: 1,
    borderBottomColor: C.line,
    paddingVertical: 12,
    gap: 6,
  },
  tableHead: { flex: 1, fontSize: 11, color: C.muted },
  cell: { flex: 1, fontSize: 12, color: C.ink },
  price: {
    flex: 1,
    fontSize: 15,
    fontWeight: "700",
    color: C.ink,
    fontVariant: ["tabular-nums"],
  },
  cellHint: { fontSize: 9, color: C.muted, marginTop: 4 },
  notice: {
    padding: 14,
    marginTop: 14,
    backgroundColor: C.pale,
    borderRadius: 12,
  },
  timeInput: {
    width: "100%",
    fontSize: 12,
    borderColor: C.line,
    borderWidth: 1,
    borderRadius: 10,
    padding: 12,
    backgroundColor: C.paper,
    color: C.ink,
  },
});
