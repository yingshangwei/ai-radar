import React, { useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator,
  AppState,
  FlatList,
  KeyboardAvoidingView,
  Linking,
  Modal,
  Platform,
  Pressable,
  RefreshControl,
  ScrollView,
  Text,
  TextInput,
  View,
} from "react-native";
import { SafeAreaProvider, SafeAreaView } from "react-native-safe-area-context";
import { StatusBar } from "expo-status-bar";
import Feather from "@expo/vector-icons/Feather";
import Svg, { Circle, Ellipse, Line, Path } from "react-native-svg";
import {
  QueryClient,
  QueryClientProvider,
  focusManager,
  useQuery,
  useInfiniteQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  api,
  APIError,
  cached,
  normalizeURL,
  saveCached,
  storage,
} from "./src/api";
import { C, s } from "./src/theme";
import { ArticleBody, ArticleByline, ReplyContext } from "./src/ArticleContent";
import { displayHeadline } from "./src/articlePresentation";
import AuthorizationCenter from "./src/AuthorizationCenter";
import DeviceReading from "./src/DeviceReading";
import {
  contentRefreshTracker,
  subscribeNativeFocus,
  syncArticleBookmark,
} from "./src/contentSync";
import { sourceConnected, sourceStatusLabel } from "./src/sourceState";
import { jobSummary, jobView, visibleJobs } from "./src/jobState";
import {
  earlySignalView,
  trialWatchView,
  discoveryStatusView,
} from "./src/discoveryView";
import appManifest from "./app.json";
import { demoArticles, demoDigest, demoStatus, demoWatches } from "./src/demo";
import type {
  Article,
  Connection,
  Digest,
  ReadResource,
  Status,
  Story,
  Watch,
} from "./src/types";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 60000 } },
});
type IconName = React.ComponentProps<typeof Feather>["name"];
const Icon = ({
  name,
  size = 20,
  color = C.ink,
}: {
  name: IconName;
  size?: number;
  color?: string;
}) => <Feather name={name} size={size} color={color} />;
const T = ({
  children,
  style,
  ...props
}: React.ComponentProps<typeof Text>) => (
  <Text {...props} style={[s.body, style]}>
    {children}
  </Text>
);
function EarlySignal({
  article,
  detail = false,
  offline = false,
}: {
  article: Article;
  detail?: boolean;
  offline?: boolean;
}) {
  const signal = earlySignalView(article.discovery, offline);
  if (!signal) return null;
  const badge = (
    <View
      style={[
        s.row,
        {
          gap: 4,
          backgroundColor: "#ECF0E6",
          borderRadius: 5,
          paddingHorizontal: 7,
          paddingVertical: 2,
        },
      ]}
    >
      <Icon name="trending-up" size={11} color={C.green} />
      <T
        style={{
          color: C.green,
          fontSize: 10,
          lineHeight: 17,
          fontWeight: "600",
        }}
      >
        {signal.label}
      </T>
    </View>
  );
  if (!detail)
    return (
      <View style={[s.row, { marginTop: 12, gap: 8 }]}>
        {badge}
        <T
          numberOfLines={1}
          style={[s.muted, { flex: 1, fontSize: 11, lineHeight: 19 }]}
        >
          {signal.reason}
        </T>
      </View>
    );
  return (
    <View style={[s.note, { marginBottom: 22, padding: 17 }]}>
      <View style={[s.row, { marginBottom: 9 }]}>{badge}</View>
      <T style={{ fontSize: 14, lineHeight: 24 }}>{signal.reason}</T>
      <T style={[s.muted, { fontSize: 12, lineHeight: 21, marginTop: 8 }]}>
        不确定点：{signal.uncertainty}
      </T>
      {signal.attention && (
        <T
          style={{ color: C.green, fontSize: 12, lineHeight: 21, marginTop: 8 }}
        >
          {signal.attention}
        </T>
      )}
      <T style={[s.muted, { fontSize: 11, lineHeight: 19, marginTop: 9 }]}>
        {signal.disclaimer}
      </T>
    </View>
  );
}
const shortDate = (value: string) => {
  const date = new Date(value);
  return `${date.getMonth() + 1}月${date.getDate()}日`;
};
const dateTime = (value: string) => {
  const date = new Date(value);
  const time = [date.getHours(), date.getMinutes(), date.getSeconds()]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
  return `${date.getFullYear()}年${shortDate(value)} ${time}`;
};
const platformName = (p: string) =>
  ({
    x: "X / Twitter",
    facebook: "Facebook",
    rss: "官方订阅",
    web: "网页来源",
  })[p] || p;
const humanError = (error: unknown) =>
  error instanceof Error ? error.message : "操作未完成，请稍后重试。";
const chineseReady = (a: Article) =>
  a.translation?.status === "ready" && !!a.title_zh;
const articleTitle = (a: Article) => (chineseReady(a) ? a.title_zh! : a.title);
const translationNote = (a: Article) => {
  switch (a.translation?.status) {
    case "ready":
      return "中文译文 · 已经模型校对，可切换原文核对";
    case "pending":
    case "running":
      return "中文版本生成中 · 当前显示原文";
    case "review_required":
      return "译文仍有疑点，待复核 · 当前显示原文";
    case "error":
      return "翻译暂未完成 · 当前显示原文";
    case "insufficient_balance":
      return "翻译账户余额不足 · 当前显示原文";
    case "disabled":
      return "翻译尚未启用 · 当前显示原文";
    default:
      return "";
  }
};

function Orbit({ size = 230 }: { size?: number }) {
  return (
    <Svg
      width={size}
      height={size}
      viewBox="0 0 240 240"
      accessibilityLabel="AI Radar 轨道图形"
    >
      <Circle
        cx="120"
        cy="120"
        r="88"
        stroke="#9BAB93"
        strokeOpacity={0.25}
        fill="none"
      />
      <Circle
        cx="120"
        cy="120"
        r="63"
        stroke="#9BAB93"
        strokeOpacity={0.25}
        fill="none"
      />
      <Ellipse
        cx="120"
        cy="120"
        rx="108"
        ry="39"
        rotation={-40}
        origin="120,120"
        stroke="#C6CFAB"
        strokeOpacity={0.7}
        fill="none"
      />
      <Ellipse
        cx="120"
        cy="120"
        rx="39"
        ry="108"
        rotation={-40}
        origin="120,120"
        stroke="#C6CFAB"
        strokeOpacity={0.35}
        fill="none"
      />
      <Line
        x1="20"
        y1="120"
        x2="220"
        y2="120"
        stroke="#B4C1A2"
        strokeOpacity={0.17}
      />
      <Line
        x1="120"
        y1="20"
        x2="120"
        y2="220"
        stroke="#B4C1A2"
        strokeOpacity={0.17}
      />
      <Circle cx="120" cy="120" r="22" fill={C.accent} />
      <Circle cx="191" cy="64" r="6" fill="#E5C894" />
      <Path d="M112 120h16m-8-8v16" stroke="#FFF7E5" strokeWidth={1.6} />
    </Svg>
  );
}
function Brand() {
  return (
    <View style={[s.row, { gap: 10 }]}>
      <T style={{ fontSize: 35, color: C.accent, lineHeight: 40 }}>✳</T>
      <View>
        <T style={s.logo}>前沿</T>
        <T style={[s.label, { fontSize: 8, letterSpacing: 2.5 }]}>AI RADAR</T>
      </View>
    </View>
  );
}
function ErrorBox({ message }: { message: string }) {
  return (
    <View accessibilityRole="alert" style={s.error}>
      <T style={{ fontSize: 13, color: "#A53B25" }}>{message}</T>
    </View>
  );
}
function Empty({
  title,
  detail,
  icon = "inbox",
}: {
  title: string;
  detail: string;
  icon?: IconName;
}) {
  return (
    <View style={s.empty}>
      <Icon name={icon} size={30} color={C.muted} />
      <T style={s.h2}>{title}</T>
      <T style={[s.muted, { textAlign: "center", maxWidth: 300 }]}>{detail}</T>
    </View>
  );
}

function Connect({ onConnect }: { onConnect: (c: Connection) => void }) {
  const [url, setUrl] = useState(
    process.env.EXPO_PUBLIC_API_URL || "https://radar.yswdra.cn",
  );
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const connect = async () => {
    setBusy(true);
    setError("");
    try {
      const c = { url: normalizeURL(url), token: token.trim() };
      if (!c.token) throw new Error("请输入设备访问令牌。");
      await api(c, "/v1/status");
      await storage.save(c);
      onConnect(c);
    } catch (e) {
      setError(humanError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <SafeAreaView style={s.screen}>
      <KeyboardAvoidingView
        style={s.container}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        <ScrollView
          contentContainerStyle={{ padding: 28, paddingBottom: 50 }}
          keyboardShouldPersistTaps="handled"
        >
          <View style={{ paddingTop: 16, paddingBottom: 38 }}>
            <Brand />
          </View>
          <View style={[s.hero, { height: 260, marginBottom: 32 }]}>
            <View style={{ position: "absolute", right: -60, top: 0 }}>
              <Orbit size={290} />
            </View>
            <T style={[s.label, { color: "#B7C1AD", marginBottom: 24 }]}>
              保持好奇，紧跟前沿。
            </T>
            <T style={s.heroTitle}>读懂 AI 的{`\n`}下一步。</T>
            <T style={{ color: "#BBC6B6", fontSize: 12, marginTop: 22 }}>
              从海量动态，到值得关注的信号。
            </T>
          </View>
          <T style={s.h2}>连接你的前沿雷达</T>
          <T style={[s.muted, { marginTop: 8, marginBottom: 25 }]}>
            每日一份简报，持续追踪重要的人与事。{`\n`}
            输入私有服务地址和设备令牌，开始阅读。
          </T>
          <T style={[s.label, { marginBottom: 9 }]}>服务地址</T>
          <TextInput
            accessibilityLabel="服务地址"
            style={s.input}
            value={url}
            onChangeText={setUrl}
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
          />
          <T style={[s.label, { marginTop: 20, marginBottom: 9 }]}>
            设备访问令牌
          </T>
          <TextInput
            accessibilityLabel="设备访问令牌"
            style={s.input}
            value={token}
            onChangeText={setToken}
            secureTextEntry
            autoCapitalize="none"
            autoCorrect={false}
            placeholder="由你的服务端提供"
            placeholderTextColor={C.muted}
          />
          {!!error && (
            <View style={{ marginTop: 15 }}>
              <ErrorBox message={error} />
            </View>
          )}
          <Pressable
            accessibilityRole="button"
            disabled={busy}
            onPress={connect}
            style={[s.button, { marginTop: 24, opacity: busy ? 0.6 : 1 }]}
          >
            {busy ? (
              <ActivityIndicator color={C.paper} />
            ) : (
              <>
                <T style={s.buttonText}>连接并开始阅读</T>
                <Icon name="arrow-up-right" color={C.paper} size={18} />
              </>
            )}
          </Pressable>
          <Pressable
            accessibilityRole="button"
            onPress={() => onConnect({ url: "", token: "", demo: true })}
            style={{ padding: 20, alignItems: "center" }}
          >
            <T style={[s.muted, { textDecorationLine: "underline" }]}>
              先看看设计示例
            </T>
          </Pressable>
          <T style={[s.muted, { fontSize: 10, textAlign: "center" }]}>
            手机令牌保存在系统安全存储中。{`\n`}
            社交平台与模型凭证只留在你的服务器。
          </T>
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

function Reader({
  connection,
  onDisconnect,
}: {
  connection: Connection;
  onDisconnect: () => void;
}) {
  const qc = useQueryClient();
  const demo = !!connection.demo;
  const [tab, setTab] = useState("today");
  const [topic, setTopic] = useState("全部");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [platform, setPlatform] = useState("");
  const [onlyPriority, setOnlyPriority] = useState(false);
  const [latest, setLatest] = useState(false);
  const [edition, setEdition] = useState("latest");
  const [history, setHistory] = useState(false);
  const [detail, setDetail] = useState<{
    story?: Story;
    article?: Article;
  } | null>(null);
  const [original, setOriginal] = useState(false);
  useEffect(() => setOriginal(false), [detail?.article?.id]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [authorizationOpen, setAuthorizationOpen] = useState(false);
  const [deviceReadingStatus, setDeviceReadingStatus] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [addWatch, setAddWatch] = useState(false);
  const [newName, setNewName] = useState("");
  const [newHandle, setNewHandle] = useState("");
  const [demoSaved, setDemoSaved] = useState<string[]>(["demo-2"]);
  const [demoDisabled, setDemoDisabled] = useState<string[]>([]);
  const [appActive, setAppActive] = useState(
    AppState.currentState !== "background",
  );
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (next) =>
      setAppActive(next === "active"),
    );
    return () => subscription.remove();
  }, []);
  useEffect(() => {
    const timer = setTimeout(() => {
      setQuery(search);
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);
  const prefix = [connection.url, demo ? "demo" : "live"];
  const status = useQuery({
    queryKey: [...prefix, "status"],
    enabled: appActive,
    staleTime: 0,
    refetchInterval: !demo && appActive ? 30000 : false,
    queryFn: async () =>
      demo
        ? { data: demoStatus, offline: false }
        : cached<Status>(connection, "/v1/status"),
  });
  const refreshContent = useMemo(
    () => contentRefreshTracker(qc, [connection.url, demo ? "demo" : "live"]),
    [qc, connection.url, connection.token, demo],
  );
  useEffect(() => {
    if (appActive && !demo) void refreshContent(status.data);
  }, [appActive, demo, refreshContent, status.data]);
  const digest = useQuery({
    queryKey: [...prefix, "digest", edition],
    queryFn: async () => {
      if (demo) return { data: demoDigest, offline: false };
      try {
        return await cached<Digest>(connection, `/v1/digests/${edition}`);
      } catch (e) {
        if (e instanceof APIError && e.status === 404)
          return { data: null, offline: false };
        throw e;
      }
    },
  });
  const editions = useQuery({
    queryKey: [...prefix, "editions"],
    enabled: history,
    queryFn: async () =>
      demo
        ? { items: [demoDigest] }
        : api<{ items: Digest[] }>(connection, "/v1/digests"),
  });
  const watches = useQuery({
    queryKey: [...prefix, "watches"],
    queryFn: async () =>
      demo
        ? { items: demoWatches }
        : api<{ items: Watch[] }>(connection, "/v1/watches"),
  });
  const params = new URLSearchParams({
    limit: "30",
    sort: latest ? "latest" : "score",
  });
  if (query) params.set("q", query);
  if (topic !== "全部") params.set("topic", topic);
  if (platform) params.set("platform", platform);
  if (onlyPriority) params.set("priority", "true");
  if (tab === "saved") params.set("saved", "true");
  const articles = useInfiniteQuery<{
    data: { items: Article[]; total: number };
    offline: boolean;
  }>({
    queryKey: [...prefix, "articles", params.toString()],
    initialPageParam: 0,
    getNextPageParam: (last, pages) =>
      pages.length * 30 < last.data.total ? pages.length * 30 : undefined,
    queryFn: async ({ pageParam }) => {
      if (!demo)
        return cached<{ items: Article[]; total: number }>(
          connection,
          `/v1/articles?${params}&offset=${pageParam}`,
        );
      const items = demoArticles
        .map((a) => ({ ...a, saved: demoSaved.includes(a.id) }))
        .filter(
          (a) =>
            (tab !== "saved" || a.saved) &&
            (!platform || a.platform === platform) &&
            (!onlyPriority || a.priority) &&
            (topic === "全部" || a.topics.includes(topic)) &&
            (!query ||
              `${a.title}${a.text}${a.author}`
                .toLowerCase()
                .includes(query.toLowerCase())),
        );
      return { data: { items, total: items.length }, offline: false };
    },
  });
  const articleDetails = useQuery({
    queryKey: [...prefix, "article", detail?.article?.id],
    enabled: !!detail?.article && !demo,
    queryFn: ({ signal }) =>
      cached<Article>(
        connection,
        `/v1/articles/${detail!.article!.id}`,
        signal,
      ),
  });
  const currentArticle =
    articleDetails.data?.data?.id === detail?.article?.id
      ? articleDetails.data?.data
      : detail?.article;
  useEffect(() => {
    if (demo) void qc.invalidateQueries({ queryKey: [...prefix, "articles"] });
  }, [demoSaved]);
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: prefix });
  };
  const action = async (fn: () => Promise<unknown>) => {
    setError("");
    setPending(true);
    try {
      await fn();
      refresh();
    } catch (e) {
      setError(humanError(e));
    } finally {
      setPending(false);
    }
  };
  const bookmark = (article: Article) =>
    action(async () => {
      let saved = !article.saved;
      if (demo) {
        setDemoSaved((x) =>
          x.includes(article.id)
            ? x.filter((id) => id !== article.id)
            : [...x, article.id],
        );
      } else {
        const result = await api<{ saved: boolean }>(
          connection,
          `/v1/articles/${article.id}/bookmark`,
          { method: "PUT", body: JSON.stringify({ saved: !article.saved }) },
        );
        saved = result.saved;
        const updated = await syncArticleBookmark(qc, prefix, article, saved);
        await saveCached(connection, `/v1/articles/${article.id}`, updated);
      }
      if (detail?.article?.id === article.id)
        setDetail({ article: { ...article, saved } });
    });
  const follow = (watch: Watch) =>
    action(async () => {
      if (demo) {
        setDemoDisabled((x) =>
          x.includes(watch.id)
            ? x.filter((id) => id !== watch.id)
            : [...x, watch.id],
        );
      } else
        await api(connection, `/v1/watches/${encodeURIComponent(watch.id)}`, {
          method: "PATCH",
          body: JSON.stringify({ enabled: !watch.enabled }),
        });
    });
  const d = digest.data?.data;
  const state = status.data?.data;
  const list = articles.data?.pages.flatMap((p) => p.data.items) || [];
  const connectedSources =
    state?.sources.filter((x) => sourceConnected(x.status)).length || 0;
  const offline =
    status.data?.offline ||
    digest.data?.offline ||
    articles.data?.pages.some((p) => p.offline);
  const dateText = shortDate(d?.date || new Date().toISOString());
  const openSource = async (url: string) => {
    try {
      await Linking.openURL(url);
    } catch {
      setError("无法打开原始来源，请检查浏览器设置。");
    }
  };
  const activeError =
    error ||
    humanErrorOrEmpty(
      tab === "today"
        ? digest.error
        : tab === "watches"
          ? watches.error
          : articles.error,
    );
  function ArticleCard({ article: a }: { article: Article }) {
    const headline = displayHeadline(a);
    const likes = a.metrics.like_count || a.metrics.reaction_count || 0;
    const analyses =
      a.resources?.filter((r) => r.status === "ready").length || 0;
    return (
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`阅读 ${a.author} 的发言：${headline.text}`}
        onPress={() => setDetail({ article: a })}
        style={({ pressed }) => [s.feedCard, pressed && { opacity: 0.78 }]}
      >
        <ArticleByline article={a} />
        <ReplyContext article={a} />
        <View style={{ marginTop: 15, marginBottom: 9 }}>
          {headline.ai && (
            <View style={[s.row, { gap: 4, marginBottom: 5 }]}>
              <Icon name="zap" size={11} color={C.green} />
              <T
                style={{
                  color: C.green,
                  fontSize: 10,
                  lineHeight: 16,
                  fontWeight: "600",
                }}
              >
                AI 摘要
              </T>
            </View>
          )}
          <T numberOfLines={2} style={s.feedTitle}>
            {headline.text}
          </T>
        </View>
        <ArticleBody article={a} preview />
        <EarlySignal article={a} />
        {!chineseReady(a) && !!translationNote(a) && (
          <T style={[s.muted, { fontSize: 10, marginTop: 10 }]}>
            {translationNote(a)}
          </T>
        )}
        <View style={[s.spread, { marginTop: 17, gap: 8 }]}>
          <View style={[s.row, { gap: 10, flex: 1, flexWrap: "wrap" }]}>
            {!!a.topics[0] && (
              <T style={{ color: C.green, fontSize: 11 }}>{a.topics[0]}</T>
            )}
            {likes > 0 && (
              <View style={[s.row, { gap: 4 }]}>
                <Icon name="heart" size={12} color={C.muted} />
                <T style={[s.muted, { fontSize: 10 }]}>
                  {likes.toLocaleString()}
                </T>
              </View>
            )}
            {analyses > 0 && (
              <View style={[s.row, { gap: 4 }]}>
                <Icon name="file-text" size={12} color={C.muted} />
                <T style={[s.muted, { fontSize: 10 }]}>{analyses} 份解读</T>
              </View>
            )}
          </View>
          <View style={[s.row, { gap: 5 }]}>
            {a.saved && <Icon name="bookmark" size={12} color={C.accent} />}
            <T style={{ fontSize: 11, lineHeight: 18, color: C.muted }}>
              阅读全文
            </T>
            <Icon name="arrow-right" size={13} color={C.muted} />
          </View>
        </View>
      </Pressable>
    );
  }
  const body = (
    <>
      <View style={s.top}>
        <Brand />
        <View style={[s.row, { gap: 12 }]}>
          <T style={s.muted}>{demo ? "设计预览" : dateText}</T>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="打开设置"
            onPress={() => setSettingsOpen(true)}
            style={s.iconButton}
          >
            <Icon name="sliders" size={18} />
          </Pressable>
        </View>
      </View>
      {demo && (
        <View
          style={{ backgroundColor: C.pale, padding: 8, alignItems: "center" }}
        >
          <T style={{ fontSize: 11, color: C.accent }}>
            设计示例 · 内容非实时新闻 · 尚未连接服务器
          </T>
        </View>
      )}
      {!!deviceReadingStatus && !demo && (
        <Pressable
          accessibilityRole="button"
          onPress={() => setAuthorizationOpen(true)}
          style={{ paddingHorizontal: 24, paddingVertical: 8 }}
        >
          <T
            style={[
              s.muted,
              {
                color: deviceReadingStatus.includes("重新验证")
                  ? C.accent
                  : C.green,
              },
            ]}
          >
            {deviceReadingStatus}
          </T>
        </Pressable>
      )}
      {offline && (
        <View style={{ paddingHorizontal: 24, paddingVertical: 8 }}>
          <T style={{ fontSize: 12, color: C.accent }}>
            当前显示离线缓存 · 下拉刷新以重新连接
          </T>
        </View>
      )}
      {state?.translation?.alert && (
        <View style={{ paddingHorizontal: 24, paddingVertical: 8 }}>
          <TranslationNotice status={state} offline={!!status.data?.offline} />
        </View>
      )}
      {!!activeError && (
        <View style={[s.pad, { paddingVertical: 10 }]}>
          <ErrorBox message={activeError} />
        </View>
      )}
      {tab === "today" ? (
        <ScrollView
          refreshControl={
            <RefreshControl
              refreshing={digest.isFetching && !digest.isLoading}
              onRefresh={refresh}
              tintColor={C.accent}
            />
          }
          contentContainerStyle={{ paddingHorizontal: 24, paddingBottom: 32 }}
        >
          <View style={[s.spread, { marginTop: 22, marginBottom: 16 }]}>
            <T style={s.label}>每天，看见新进展</T>
            <Pressable
              accessibilityRole="button"
              onPress={() => setHistory(true)}
              style={[s.row, { gap: 5 }]}
            >
              <Icon name="calendar" size={13} color={C.muted} />
              <T style={s.muted}>往期日报</T>
            </Pressable>
          </View>
          <View style={s.hero}>
            <View
              style={{
                position: "absolute",
                right: -62,
                top: 14,
                opacity: 0.9,
              }}
            >
              <Orbit />
            </View>
            <T style={[s.label, { color: "#B7C1AD", marginBottom: 19 }]}>
              人工智能 · 每日简报
            </T>
            <T style={s.heroTitle}>保持好奇。{`\n`}看见下一步。</T>
            <View style={[s.row, { gap: 8, marginTop: 22 }]}>
              <View
                style={{
                  width: 5,
                  height: 5,
                  borderRadius: 3,
                  backgroundColor: "#CDDBA4",
                }}
              />
              <T style={{ fontSize: 11, color: "#CCD3C1" }}>
                {d
                  ? `${d.source_count} 条来源 · ${d.stories.length} 个值得关注的信号`
                  : "每一天，让重要的信息浮现"}
              </T>
            </View>
          </View>
          <View
            style={[
              s.spread,
              {
                paddingVertical: 21,
                borderBottomWidth: 1,
                borderColor: C.line,
              },
            ]}
          >
            <View>
              <T style={s.label}>
                {d &&
                d.date !==
                  new Date().toLocaleDateString("sv-SE", {
                    timeZone: state?.timezone || "Asia/Shanghai",
                  })
                  ? "历史简报"
                  : "今日简报"}
              </T>
              <T style={{ fontSize: 13, marginTop: 4 }}>
                {d ? shortDate(d.date) : "等待第一份日报"}
              </T>
            </View>
            <View style={{ alignItems: "flex-end" }}>
              <T style={[s.label, { color: demo ? C.muted : C.green }]}>
                {demo ? "示例模式" : `${connectedSources} 个来源已连接`}
              </T>
              <T style={s.muted}>
                {state?.daily_time || "08:00"} ·{" "}
                {state?.timezone || "Asia/Shanghai"}
              </T>
            </View>
          </View>
          {digest.isLoading ? (
            <View style={{ padding: 40 }}>
              <ActivityIndicator color={C.accent} />
            </View>
          ) : d ? (
            <>
              <View style={s.section}>
                <T style={s.h2}>{d.title}</T>
                <T
                  style={[
                    s.body,
                    { marginTop: 13, color: "#62685D", lineHeight: 28 },
                  ]}
                >
                  {d.overview}
                </T>
              </View>
              <View style={[s.spread, { paddingVertical: 10 }]}>
                <T style={s.sectionTitle}>值得关注</T>
                <T style={s.label}>值得关注的动态</T>
              </View>
              {d.stories.map((story, i) => (
                <Pressable
                  key={`${story.title}-${i}`}
                  accessibilityRole="button"
                  onPress={() => setDetail({ story })}
                  style={[s.card, { flexDirection: "row", gap: 15 }]}
                >
                  <T
                    style={{
                      color: C.accent,
                      fontSize: 15,
                      fontWeight: "500",
                      paddingTop: 2,
                      fontVariant: ["tabular-nums"],
                    }}
                  >
                    {String(i + 1).padStart(2, "0")}
                  </T>
                  <View style={{ flex: 1 }}>
                    <View style={[s.spread, { marginBottom: 9 }]}>
                      <T
                        style={[s.label, { color: C.accent, letterSpacing: 1 }]}
                      >
                        {story.category}
                      </T>
                      <Icon name="arrow-up-right" size={16} color={C.muted} />
                    </View>
                    <T style={s.cardTitle}>{story.title}</T>
                    <T
                      numberOfLines={3}
                      style={[
                        s.muted,
                        { fontSize: 13, lineHeight: 23, marginTop: 9 },
                      ]}
                    >
                      {story.summary}
                    </T>
                    <T style={[s.muted, { marginTop: 12, fontSize: 10 }]}>
                      {story.source_ids.length} 个原始来源 · 点击展开
                    </T>
                  </View>
                </Pressable>
              ))}
              <View style={[s.note, { marginTop: 26 }]}>
                <T style={[s.muted, { fontSize: 11 }]}>
                  {d.provider === "no_updates"
                    ? "本期为来源状态汇报，未调用 AI 生成内容。"
                    : d.provider === "extractive"
                      ? "当前为原文摘录，未生成 AI 分析。"
                      : demo
                        ? "以上内容为虚构设计示例。"
                        : "由 AI 整理，保留原始引用。判断与推测请以原文为准。"}
                  {`\n`}统计至 {dateTime(d.window_end)}。
                </T>
              </View>
              {d.coverage.some((x) => x.status !== "healthy") && !demo && (
                <Pressable
                  onPress={() => setSettingsOpen(true)}
                  style={{ paddingVertical: 14 }}
                >
                  <T style={{ fontSize: 12, color: C.accent }}>
                    部分来源尚未完整覆盖 · 查看覆盖范围 →
                  </T>
                </Pressable>
              )}
            </>
          ) : (
            <Empty
              title="第一份日报，即将开始"
              detail="连接信息源并完成首次采集后，服务会在每日指定时间整理简报。可在设置中查看来源状态。"
              icon="sunrise"
            />
          )}
          <T
            style={[
              s.label,
              { textAlign: "center", fontSize: 9, marginTop: 35 },
            ]}
          >
            少些噪音，多些洞见。
          </T>
        </ScrollView>
      ) : tab === "watches" ? (
        <ScrollView
          contentContainerStyle={{ paddingHorizontal: 24, paddingBottom: 32 }}
          refreshControl={
            <RefreshControl
              refreshing={watches.isRefetching}
              onRefresh={refresh}
            />
          }
        >
          <View style={s.section}>
            <T style={s.h1}>关注重要的人。</T>
            <T style={[s.muted, { marginTop: 8 }]}>
              从一手动态，感知思想、技术与产品的变化。
            </T>
          </View>
          <View style={[s.spread, { marginBottom: 12 }]}>
            <T style={s.label}>
              {watches.data?.items.filter((w) =>
                demo ? !demoDisabled.includes(w.id) : w.enabled,
              ).length || 0}{" "}
              个账号正在关注
            </T>
            <Pressable
              accessibilityRole="button"
              onPress={() => setAddWatch(true)}
              style={[s.smallButton, s.row, { gap: 5 }]}
            >
              <Icon name="plus" size={14} />
              <T style={{ fontSize: 12 }}>添加账号</T>
            </Pressable>
          </View>
          {watches.data?.items.map((w) => {
            const enabled = demo ? !demoDisabled.includes(w.id) : w.enabled;
            const trial = trialWatchView(
              w.discovery,
              !!status.data?.offline || !!watches.error,
            );
            return (
              <View
                key={w.id}
                style={[
                  s.spread,
                  {
                    paddingVertical: 19,
                    borderBottomWidth: 1,
                    borderColor: C.line,
                    gap: 12,
                  },
                ]}
              >
                <View
                  style={[
                    s.avatar,
                    {
                      backgroundColor:
                        w.organization === "Anthropic"
                          ? "#EFE0D0"
                          : w.organization === "Google"
                            ? "#E2E9EA"
                            : "#E4E8DB",
                    },
                  ]}
                >
                  <T style={s.avatarText}>{w.name.slice(0, 2)}</T>
                </View>
                <View style={{ flex: 1 }}>
                  <T style={{ fontSize: 15, fontWeight: "600" }}>{w.name}</T>
                  <T style={[s.muted, { fontSize: 11 }]}>@{w.handle}</T>
                  <T style={[s.muted, { fontSize: 10 }]}>{w.role}</T>
                  {trial && (
                    <View style={{ marginTop: 5 }}>
                      <T
                        style={{ color: C.green, fontSize: 10, lineHeight: 18 }}
                      >
                        {trial.label}
                      </T>
                      {!!trial.reason && (
                        <T
                          numberOfLines={2}
                          style={[s.muted, { fontSize: 10, lineHeight: 18 }]}
                        >
                          {trial.reason}
                        </T>
                      )}
                    </View>
                  )}
                </View>
                <Pressable
                  accessibilityRole="switch"
                  accessibilityState={{ checked: enabled }}
                  accessibilityLabel={`${enabled ? "取消关注" : "关注"} ${w.name}`}
                  disabled={pending}
                  onPress={() => follow(w)}
                  style={[
                    s.smallButton,
                    enabled
                      ? { backgroundColor: C.ink, borderColor: C.ink }
                      : {},
                  ]}
                >
                  <T
                    style={{ color: enabled ? C.paper : C.muted, fontSize: 11 }}
                  >
                    {enabled ? "已关注" : "关注"}
                  </T>
                </Pressable>
              </View>
            );
          })}
          <View style={[s.note, { marginTop: 22 }]}>
            <T style={s.muted}>
              重点账号降低热度门槛，仍会过滤与 AI
              无关的动态。账号身份与简介可根据实际情况调整。
            </T>
          </View>
        </ScrollView>
      ) : (
        <FlatList
          data={list}
          keyExtractor={(a) => a.id}
          renderItem={({ item }) => <ArticleCard article={item} />}
          contentContainerStyle={{ paddingHorizontal: 24, paddingBottom: 30 }}
          keyboardShouldPersistTaps="handled"
          refreshControl={
            <RefreshControl
              refreshing={articles.isRefetching}
              onRefresh={refresh}
              tintColor={C.accent}
            />
          }
          ListHeaderComponent={
            <>
              <View style={s.section}>
                <T style={s.h1}>
                  {tab === "saved" ? "留下值得回看的。" : "前沿，持续发生。"}
                </T>
                <T style={[s.muted, { marginTop: 8 }]}>
                  {tab === "saved"
                    ? "你的收藏，构成自己的知识线索。"
                    : "模型、产品与思想的最新信号，在这里汇合。"}
                </T>
              </View>
              <View
                style={[
                  s.row,
                  s.input,
                  { gap: 10, paddingVertical: 0, marginBottom: 17 },
                ]}
              >
                <Icon name="search" size={17} color={C.muted} />
                <TextInput
                  accessibilityLabel="搜索信息"
                  placeholder="搜索主题、人物或关键词"
                  placeholderTextColor={C.muted}
                  value={search}
                  onChangeText={setSearch}
                  style={{
                    flex: 1,
                    paddingVertical: 14,
                    color: C.ink,
                    fontSize: 13,
                  }}
                />
              </View>
              <ScrollView
                horizontal
                showsHorizontalScrollIndicator={false}
                contentContainerStyle={{ gap: 8, paddingBottom: 16 }}
              >
                {[
                  "全部",
                  "前瞻",
                  "学界",
                  "模型",
                  "产品",
                  "技术",
                  "开源",
                  "观点",
                  "产业",
                ].map((t) => (
                  <Pressable
                    key={t}
                    onPress={() => {
                      setTopic(t);
                    }}
                    accessibilityRole="button"
                    accessibilityState={{ selected: topic === t }}
                    style={[s.pill, topic === t && s.pillActive]}
                  >
                    <T style={[s.pillText, topic === t && { color: C.paper }]}>
                      {t}
                    </T>
                  </Pressable>
                ))}
              </ScrollView>
              <View
                style={[
                  s.spread,
                  {
                    paddingBottom: 14,
                    borderBottomWidth: 1,
                    borderColor: C.line,
                  },
                ]}
              >
                <Pressable
                  onPress={() => setOnlyPriority(!onlyPriority)}
                  style={[s.row, { gap: 5 }]}
                >
                  <Icon
                    name="star"
                    size={13}
                    color={onlyPriority ? C.accent : C.muted}
                  />
                  <T style={[s.muted, onlyPriority && { color: C.accent }]}>
                    仅重点
                  </T>
                </Pressable>
                <Pressable onPress={() => setLatest(!latest)}>
                  <T style={s.muted}>{latest ? "最新发布" : "热度优先"} ⇅</T>
                </Pressable>
                <Pressable
                  onPress={() =>
                    setPlatform((p) =>
                      p === ""
                        ? "x"
                        : p === "x"
                          ? "facebook"
                          : p === "facebook"
                            ? "rss"
                            : "",
                    )
                  }
                >
                  <T style={s.muted}>
                    {platform ? platformName(platform) : "全部来源"} ⌄
                  </T>
                </Pressable>
              </View>
            </>
          }
          ListEmptyComponent={
            articles.isLoading ? (
              <ActivityIndicator style={{ padding: 40 }} color={C.accent} />
            ) : (
              <Empty
                title={
                  tab === "saved"
                    ? "把有价值的信息留下来"
                    : "暂时没有匹配的信号"
                }
                detail={
                  tab === "saved"
                    ? "阅读文章时点击收藏，下次就能在这里找到。"
                    : "试试其他关键词或筛选条件，也可以在设置中检查来源连接。"
                }
              />
            )
          }
          ListFooterComponent={
            articles.hasNextPage ? (
              <Pressable
                disabled={articles.isFetchingNextPage}
                onPress={() => void articles.fetchNextPage()}
                style={[s.smallButton, { alignSelf: "center", marginTop: 20 }]}
              >
                <T style={s.muted}>
                  {articles.isFetchingNextPage ? "加载中…" : "加载更多"}
                </T>
              </Pressable>
            ) : null
          }
        />
      )}
      <View style={s.nav}>
        {(
          [
            { id: "today", label: "今日", icon: "sun" },
            { id: "radar", label: "雷达", icon: "radio" },
            { id: "watches", label: "关注", icon: "users" },
            { id: "saved", label: "收藏", icon: "bookmark" },
          ] as { id: string; label: string; icon: IconName }[]
        ).map((t) => (
          <Pressable
            accessibilityRole="tab"
            accessibilityState={{ selected: tab === t.id }}
            key={t.id}
            onPress={() => {
              setTab(t.id);
              setError("");
            }}
            style={s.navItem}
          >
            <Icon
              name={t.icon}
              size={21}
              color={tab === t.id ? C.accent : C.muted}
            />
            <T
              style={[
                s.navText,
                tab === t.id && { color: C.accent, fontWeight: "700" },
              ]}
            >
              {t.label}
            </T>
          </Pressable>
        ))}
      </View>
    </>
  );
  return (
    <SafeAreaView style={s.screen}>
      <View style={s.container}>{body}</View>
      <Sheet open={!!detail} onClose={() => setDetail(null)} title="深入阅读">
        <TranslationNotice status={state} offline={!!status.data?.offline} />
        {detail && (
          <>
            {detail.story ? (
              <>
                <T style={[s.label, { color: C.accent, marginBottom: 14 }]}>
                  {detail.story.category} / 前沿观察
                </T>
                <T style={s.h1}>{detail.story.title}</T>
                <T
                  style={[
                    s.body,
                    { marginTop: 25, lineHeight: 30, fontSize: 16 },
                  ]}
                >
                  {detail.story.summary}
                </T>
                <View style={[s.note, { marginVertical: 28, padding: 20 }]}>
                  <T style={[s.label, { color: C.green, marginBottom: 10 }]}>
                    为什么值得关注
                  </T>
                  <T style={s.body}>{detail.story.why_it_matters}</T>
                </View>
                <T style={s.sectionTitle}>回到一手来源</T>
                {detail.story.source_ids.map((uid) => {
                  const a = d?.sources?.find((x) => x.id === uid);
                  return a ? (
                    <View key={uid} style={s.card}>
                      <T style={s.muted}>
                        {platformName(a.platform)} · {a.author}
                      </T>
                      <T style={[s.cardTitle, { fontSize: 16, marginTop: 6 }]}>
                        {articleTitle(a)}
                      </T>
                      <View style={[s.spread, { marginTop: 12 }]}>
                        <Pressable onPress={() => setDetail({ article: a })}>
                          <T style={{ color: C.accent, fontSize: 12 }}>
                            阅读中英对照 →
                          </T>
                        </Pressable>
                        <Pressable
                          onPress={() => openSource(a.url)}
                          accessibilityLabel={`打开 ${a.author} 原始来源`}
                        >
                          <Icon name="external-link" size={17} />
                        </Pressable>
                      </View>
                    </View>
                  ) : (
                    <T key={uid} style={s.muted}>
                      来源暂不可用
                    </T>
                  );
                })}
              </>
            ) : currentArticle ? (
              <>
                <ArticleByline article={currentArticle} detail />
                <ReplyContext article={currentArticle} onOpen={openSource} />
                <View style={{ marginTop: 22, marginBottom: 20 }}>
                  {displayHeadline(currentArticle).ai && (
                    <View style={[s.row, { gap: 5, marginBottom: 8 }]}>
                      <Icon name="zap" size={12} color={C.green} />
                      <T
                        style={{
                          color: C.green,
                          fontSize: 11,
                          fontWeight: "600",
                        }}
                      >
                        AI 摘要
                      </T>
                    </View>
                  )}
                  <T
                    style={{
                      color: C.ink,
                      fontSize: 26,
                      lineHeight: 37,
                      fontWeight: "700",
                    }}
                  >
                    {displayHeadline(currentArticle).text}
                  </T>
                </View>
                <EarlySignal
                  article={currentArticle}
                  detail
                  offline={
                    !!articleDetails.data?.offline || !!status.data?.offline
                  }
                />
                <View
                  style={[
                    s.spread,
                    {
                      borderTopWidth: 1,
                      borderColor: C.line,
                      paddingTop: 16,
                      marginBottom: 20,
                    },
                  ]}
                >
                  <T style={[s.label, { letterSpacing: 1 }]}>完整发言</T>
                  {chineseReady(currentArticle) && (
                    <View
                      style={[
                        s.row,
                        {
                          gap: 3,
                          backgroundColor: "#EAECE3",
                          padding: 3,
                          borderRadius: 9,
                        },
                      ]}
                    >
                      {([false, true] as const).map((value) => (
                        <Pressable
                          key={String(value)}
                          accessibilityRole="button"
                          accessibilityState={{ selected: original === value }}
                          onPress={() => setOriginal(value)}
                          style={{
                            paddingVertical: 7,
                            paddingHorizontal: 14,
                            borderRadius: 7,
                            backgroundColor:
                              original === value ? C.paper : "transparent",
                          }}
                        >
                          <T
                            style={{
                              fontSize: 12,
                              fontWeight: original === value ? "600" : "400",
                              color: original === value ? C.ink : C.muted,
                            }}
                          >
                            {value ? "原文" : "中文"}
                          </T>
                        </Pressable>
                      ))}
                    </View>
                  )}
                </View>
                {currentArticle.text.trim() === currentArticle.title.trim() &&
                currentArticle.platform !== "x" &&
                currentArticle.platform !== "facebook" ? (
                  <T style={s.body}>
                    此来源仅提供标题，可打开原始链接阅读全文。
                  </T>
                ) : (
                  <ArticleBody article={currentArticle} original={original} />
                )}
                {!!translationNote(currentArticle) && (
                  <T style={[s.muted, { marginTop: 20, fontSize: 10 }]}>
                    {translationNote(currentArticle)}
                  </T>
                )}
                {!!currentArticle?.resources?.length && (
                  <View style={{ marginTop: 28, marginBottom: 8 }}>
                    <T style={s.sectionTitle}>网页与文章解读</T>
                    <T
                      style={[
                        s.muted,
                        { marginTop: 7, marginBottom: 18, fontSize: 11 },
                      ]}
                    >
                      只读原文与直接关联内容，保留来源，方便核对。
                    </T>
                    {articleDetails.data?.offline && (
                      <T style={[s.muted, { marginBottom: 12 }]}>
                        当前显示已保存的离线内容。
                      </T>
                    )}
                    {currentArticle.resources.map((resource) => (
                      <ResourceCard
                        key={resource.id}
                        resource={resource}
                        onOpen={openSource}
                      />
                    ))}
                  </View>
                )}
                <View style={[s.row, { gap: 12, marginTop: 28 }]}>
                  <Pressable
                    disabled={pending}
                    onPress={() => bookmark(currentArticle!)}
                    style={[s.button, { flex: 1 }]}
                  >
                    <Icon name="bookmark" color={C.paper} size={17} />
                    <T style={s.buttonText}>
                      {currentArticle.saved ? "取消收藏" : "收藏文章"}
                    </T>
                  </Pressable>
                  <Pressable
                    onPress={() => openSource(currentArticle!.url)}
                    style={[
                      s.button,
                      {
                        backgroundColor: C.paper,
                        borderWidth: 1,
                        borderColor: C.line,
                        flex: 1,
                      },
                    ]}
                  >
                    <T style={[s.buttonText, { color: C.ink }]}>查看来源</T>
                    <Icon name="external-link" size={17} />
                  </Pressable>
                </View>
                <T
                  selectable
                  style={[s.muted, { fontSize: 10, marginTop: 18 }]}
                >
                  {currentArticle.url}
                </T>
              </>
            ) : null}
          </>
        )}
        {!!error && (
          <View style={{ marginTop: 20 }}>
            <ErrorBox message={error} />
          </View>
        )}
      </Sheet>
      <Sheet open={history} onClose={() => setHistory(false)} title="往期日报">
        {editions.isLoading ? (
          <ActivityIndicator />
        ) : editions.data?.items.length ? (
          editions.data.items.map((e) => (
            <Pressable
              key={e.date}
              style={s.card}
              onPress={() => {
                setEdition(e.date);
                setHistory(false);
              }}
            >
              <T style={[s.label, { color: C.accent, marginBottom: 10 }]}>
                {e.date}
              </T>
              <T style={s.h2}>{e.title}</T>
              <T style={s.muted}>
                {e.stories.length} 条精选 · {e.source_count} 个来源
              </T>
            </Pressable>
          ))
        ) : (
          <Empty
            title="日报会在这里积累"
            detail="每日生成的简报都会保留，方便回顾。"
          />
        )}
        {editions.error && <ErrorBox message={humanError(editions.error)} />}
      </Sheet>
      <Sheet
        open={addWatch}
        onClose={() => setAddWatch(false)}
        title="添加重点关注"
      >
        <T style={s.muted}>输入 X 账号名称与 handle，无需包含 @。</T>
        <TextInput
          accessibilityLabel="账号名称"
          value={newName}
          onChangeText={setNewName}
          placeholder="名称，例如 Anthropic"
          style={[s.input, { marginTop: 20 }]}
        />
        <TextInput
          accessibilityLabel="X 账号 handle"
          value={newHandle}
          onChangeText={setNewHandle}
          placeholder="账号，例如 AnthropicAI"
          autoCapitalize="none"
          autoCorrect={false}
          style={[s.input, { marginTop: 12 }]}
        />
        <Pressable
          disabled={pending}
          style={[s.button, { marginTop: 24 }]}
          onPress={() =>
            action(async () => {
              if (demo)
                throw new Error("设计示例不保存新账号，请先连接服务器。");
              if (!newName.trim() || !newHandle.trim())
                throw new Error("请填写名称与账号。");
              await api(connection, "/v1/watches", {
                method: "POST",
                body: JSON.stringify({
                  name: newName.trim(),
                  handle: newHandle.trim().replace(/^@/, ""),
                }),
              });
              setAddWatch(false);
              setNewName("");
              setNewHandle("");
            })
          }
        >
          <T style={s.buttonText}>添加关注</T>
        </Pressable>
        {!!error && (
          <View style={{ marginTop: 20 }}>
            <ErrorBox message={error} />
          </View>
        )}
      </Sheet>
      <Sheet
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        title="你的雷达"
      >
        <TranslationNotice status={state} offline={!!status.data?.offline} />
        <View style={[s.note, { marginBottom: 25 }]}>
          <T style={s.label}>连接状态</T>
          <T style={[s.body, { marginTop: 7 }]}>
            {demo ? "设计示例模式" : connection.url}
          </T>
          <T style={s.muted}>
            {demo
              ? "尚未接通任何实时来源"
              : `${state?.article_count || 0} 条已收录 · ${state?.provider || "尚未获取"} 摘要引擎`}
          </T>
        </View>
        {state?.translation?.enabled && (
          <View style={[s.note, { marginBottom: 25 }]}>
            <T style={s.label}>中文阅读</T>
            <T style={[s.body, { marginTop: 7 }]}>
              {state.translation.counts.ready || 0} 条已有中文版本
            </T>
            <T style={s.muted}>
              {Object.entries(state.translation.counts).reduce(
                (n, [key, value]) => n + (key === "ready" ? 0 : value),
                0,
              )}{" "}
              条等待翻译或复核 · 原文随时可查
            </T>
            {state.translation.queue && (
              <>
                <T style={[s.body, { marginTop: 12 }]}>
                  {status.data?.offline ? "离线 · 上次队列状态：" : ""}
                  正在处理 {state.translation.queue.counts.active || 0} 份 ·
                  待处理 {state.translation.queue.counts.runnable || 0} 份 ·
                  等待重试 {state.translation.queue.counts.retrying || 0} 份
                </T>
                {!!state.translation.queue.counts.needs_attention && (
                  <T style={[s.muted, { marginTop: 7 }]}>
                    {state.translation.queue.counts.needs_attention}{" "}
                    份需要检查：审核未通过或调用结果待确认，进度已保留。
                  </T>
                )}
                <T style={[s.muted, { marginTop: 7 }]}>
                  按主消息和网页正文统计，重复内容仅计一份。
                  {status.data?.offline
                    ? "当前进度尚未确认，联网后会自动更新。"
                    : "可执行的内容会自动续跑，无需反复点击采集。"}
                </T>
              </>
            )}
          </View>
        )}
        <T style={s.sectionTitle}>信息源</T>
        {!demo && (
          <Pressable
            style={[s.button, { marginTop: 16, marginBottom: 18 }]}
            onPress={() => {
              setSettingsOpen(false);
              setAuthorizationOpen(true);
            }}
          >
            <T style={s.buttonText}>网页采集中心</T>
          </Pressable>
        )}
        <T style={[s.muted, { marginTop: 7 }]}>
          授权、采集异常与最近成功时间会在这里显示。
        </T>
        {state?.sources.map((source) => (
          <View key={source.id} style={s.card}>
            <View style={s.spread}>
              <T style={{ fontWeight: "600" }}>{source.name}</T>
              <View style={[s.row, { gap: 6 }]}>
                <View
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: 3,
                    backgroundColor:
                      source.status === "healthy" ? C.green : C.accent,
                  }}
                />
                <T
                  style={[
                    s.muted,
                    { color: source.status === "healthy" ? C.green : C.accent },
                  ]}
                >
                  {sourceStatusLabel(source.status)}
                </T>
              </View>
            </View>
            <T style={[s.muted, { marginTop: 7 }]}>{source.message}</T>
            {source.last_success_at && (
              <T style={[s.muted, { fontSize: 10, marginTop: 5 }]}>
                最近成功：
                {dateTime(source.last_success_at)}
              </T>
            )}
          </View>
        ))}
        <View style={s.section}>
          <T style={s.sectionTitle}>每日汇报</T>
          <T style={[s.muted, { marginTop: 9 }]}>
            {state?.daily_time || "08:00"} ·{" "}
            {state?.timezone || "Asia/Shanghai"}
            {`\n`}
            {state?.scheduler_enabled
              ? "自动采集和日报已开启"
              : "自动任务未开启，需在服务器配置中启用"}
          </T>
        </View>
        {state?.discovery && (
          <View style={{ marginBottom: 23 }}>
            <T style={s.sectionTitle}>关联发现与潜力判断</T>
            <T style={[s.muted, { marginTop: 7, fontSize: 12 }]}>
              {discoveryStatusView(state.discovery, !!status.data?.offline)}
            </T>
          </View>
        )}
        <T style={s.sectionTitle}>后台任务</T>
        <T style={[s.muted, { marginTop: 7, marginBottom: 12 }]}>
          {status.data?.offline ? "离线 · 上次状态：" : ""}
          {state ? jobSummary(state) : "正在获取任务状态…"}
        </T>
        {!!status.data?.offline && (
          <T style={[s.muted, { marginBottom: 12 }]}>
            当前进度尚未确认，联网后会自动更新。
          </T>
        )}
        {visibleJobs(state?.jobs || []).map((j) => {
          const details = jobView(j, !!status.data?.offline, state?.server_now);
          return (
            <View key={j.id} style={[s.note, { marginBottom: 9 }]}>
              <T style={{ fontWeight: "600" }}>{details.title}</T>
              <T style={{ fontSize: 12, marginTop: 6 }}>{details.message}</T>
              {details.phase && <T style={s.muted}>{details.phase}</T>}
              {details.attempt && <T style={s.muted}>{details.attempt}</T>}
              {details.times.map((time) => (
                <T
                  key={time.label}
                  style={[s.muted, { fontSize: 10, marginTop: 4 }]}
                >
                  {time.label}：{dateTime(time.value)}
                </T>
              ))}
              {details.responseNote && (
                <T style={[s.muted, { fontSize: 10, marginTop: 4 }]}>
                  {details.responseNote}
                </T>
              )}
            </View>
          );
        })}
        <Pressable
          disabled={pending || demo}
          onPress={() =>
            action(async () => {
              await api(connection, "/v1/admin/jobs?kind=daily", {
                method: "POST",
              });
              setError("任务已提交，进度和完成结果会自动更新。");
            })
          }
          style={[
            s.smallButton,
            { alignItems: "center", marginTop: 20, opacity: demo ? 0.4 : 1 },
          ]}
        >
          <T style={s.muted}>立即采集并汇报（需管理令牌）</T>
        </Pressable>
        {!!error && (
          <View style={{ marginTop: 15 }}>
            <ErrorBox message={error} />
          </View>
        )}
        <Pressable
          onPress={() => {
            setSettingsOpen(false);
            onDisconnect();
          }}
          style={[s.button, { marginTop: 30 }]}
        >
          <T style={s.buttonText}>
            {demo ? "连接我的服务器" : "退出并更换连接"}
          </T>
        </Pressable>
        <T
          style={[s.label, { textAlign: "center", marginTop: 28, fontSize: 9 }]}
        >
          AI RADAR / 前沿 · {appManifest.expo.version}
        </T>
      </Sheet>
      <DeviceReading
        connection={connection}
        paused={authorizationOpen}
        onStatus={setDeviceReadingStatus}
        onComplete={refresh}
      />
      <AuthorizationCenter
        connection={connection}
        open={authorizationOpen}
        onClose={() => setAuthorizationOpen(false)}
        onRefresh={refresh}
      />
    </SafeAreaView>
  );
}
function humanErrorOrEmpty(error: unknown) {
  return error ? humanError(error) : "";
}
function ResourceCard({
  resource: r,
  onOpen,
}: {
  resource: ReadResource;
  onOpen: (url: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [original, setOriginal] = useState(false);
  const domain = (() => {
    try {
      return new URL(r.resolved_url).hostname;
    } catch {
      return "来源网页";
    }
  })();
  const waiting =
    (
      {
        pending: "正在等待正文读取或内容解读。",
        analysis_error: "正文已保存，解读暂未完成，后续任务会重试。",
        auth_required:
          "目标网页要求登录，可到“你的雷达 → 网页采集中心”用手机读取。",
        access_restricted: "网站限制服务器访问，可到网页采集中心用手机读取。",
        restricted: "网站限制自动读取，尚未取得正文。",
        blocked: "该链接不是可读取的公开网页。",
        unavailable: "正文暂时无法读取。",
        rate_limited: "访问频率受限，稍后重试。",
      } as Record<string, string>
    )[r.status] || "暂未完成内容解读。";
  return (
    <View style={[s.note, { marginBottom: 16, padding: 20 }]}>
      <T style={[s.label, { color: C.green, marginBottom: 10 }]}>
        {r.relation === "source"
          ? "原文正文"
          : r.relation === "mention"
            ? "文中提及"
            : "直接关联"}{" "}
        · {domain}
        {r.capture_method === "mobile_browser"
          ? " · 手机读取"
          : r.capture_method === "manual"
            ? " · 手动选取"
            : ""}
      </T>
      <T style={[s.cardTitle, { fontSize: 19 }]}>{r.title_zh || r.title}</T>
      {r.status === "ready" ? (
        <>
          <T selectable style={[s.body, { marginTop: 16, lineHeight: 27 }]}>
            {r.summary_zh}
          </T>
          <View style={{ marginTop: 16, gap: 10 }}>
            {r.key_points_zh.map((point, i) => (
              <View key={i} style={{ flexDirection: "row", gap: 10 }}>
                <T style={{ color: C.accent }}>•</T>
                <T selectable style={[s.body, { flex: 1, lineHeight: 25 }]}>
                  {point}
                </T>
              </View>
            ))}
          </View>
          {!!r.why_it_matters_zh && (
            <View style={{ marginTop: 18 }}>
              <T style={[s.label, { marginBottom: 8 }]}>值得关注</T>
              <T selectable style={[s.body, { lineHeight: 25 }]}>
                {r.why_it_matters_zh}
              </T>
            </View>
          )}
        </>
      ) : (
        <T style={[s.muted, { marginTop: 14 }]}>{r.message || waiting}</T>
      )}
      {r.partial && (
        <T style={[s.muted, { marginTop: 14 }]}>
          已取得部分正文，解读仅依据这部分内容。
        </T>
      )}
      {r.status === "ready" && r.fetch_status !== "fetched" && (
        <T style={[s.muted, { marginTop: 14 }]}>
          当前使用上次保存的正文。{r.message}
        </T>
      )}
      <View style={[s.spread, { marginTop: 20 }]}>
        <Pressable
          accessibilityRole="button"
          onPress={() => setExpanded(!expanded)}
          disabled={!r.text}
        >
          <T style={{ color: r.text ? C.accent : C.muted, fontSize: 12 }}>
            {expanded
              ? "收起正文 ↑"
              : r.text
                ? "查看保存的正文 ↓"
                : r.fetched_at
                  ? "正文暂未加载"
                  : "正文尚未保存"}
          </T>
        </Pressable>
        <Pressable
          accessibilityRole="link"
          onPress={() => onOpen(r.resolved_url)}
        >
          <T style={{ color: C.accent, fontSize: 12 }}>打开来源 ↗</T>
        </Pressable>
      </View>
      {expanded && (
        <View style={{ marginTop: 20 }}>
          {r.text_zh ? (
            <Pressable onPress={() => setOriginal(!original)}>
              <T style={{ color: C.accent, marginBottom: 12 }}>
                {original ? "切换中文" : "切换原文"}
              </T>
            </Pressable>
          ) : (
            <T style={[s.muted, { marginBottom: 12 }]}>
              中文正文尚待翻译或校对，当前显示原文。
            </T>
          )}
          <T selectable style={[s.body, { lineHeight: 27 }]}>
            {original || !r.text_zh ? r.text : r.text_zh}
          </T>
        </View>
      )}
    </View>
  );
}

function TranslationNotice({
  status,
  offline,
}: {
  status?: Status;
  offline: boolean;
}) {
  const alert = status?.translation?.alert;
  if (!alert) return null;
  return (
    <View
      accessibilityRole="alert"
      accessibilityLiveRegion="polite"
      style={[
        s.note,
        { borderLeftWidth: 3, borderLeftColor: C.accent, marginBottom: 12 },
      ]}
    >
      <T style={{ color: C.accent, fontWeight: "700", marginBottom: 5 }}>
        {offline ? "上次记录：" : ""}
        {alert.title}
      </T>
      <T style={[s.muted, { lineHeight: 21 }]}>{alert.message}</T>
      {offline && (
        <T style={[s.muted, { fontSize: 10, marginTop: 5 }]}>
          当前离线，恢复网络后更新状态。
        </T>
      )}
    </View>
  );
}

function Sheet({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <Modal
      visible={open}
      animationType="slide"
      onRequestClose={onClose}
      presentationStyle="pageSheet"
    >
      <SafeAreaView style={s.screen}>
        <View style={s.container}>
          <View style={[s.spread, { padding: 24 }]}>
            <T style={s.sectionTitle}>{title}</T>
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="关闭"
              onPress={onClose}
              style={s.iconButton}
            >
              <Icon name="x" />
            </Pressable>
          </View>
          <ScrollView
            keyboardShouldPersistTaps="handled"
            contentContainerStyle={{ paddingHorizontal: 26, paddingBottom: 50 }}
          >
            {children}
          </ScrollView>
        </View>
      </SafeAreaView>
    </Modal>
  );
}
export default function App() {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [ready, setReady] = useState(false);
  useEffect(
    () => subscribeNativeFocus(AppState, Platform.OS, focusManager),
    [],
  );
  useEffect(() => {
    storage
      .load()
      .then(setConnection)
      .catch(() => {})
      .finally(() => setReady(true));
  }, []);
  return (
    <SafeAreaProvider>
      <QueryClientProvider client={queryClient}>
        <StatusBar style="dark" />
        {!ready ? (
          <View style={[s.screen, { justifyContent: "center" }]}>
            <ActivityIndicator color={C.accent} />
          </View>
        ) : connection ? (
          <Reader
            connection={connection}
            onDisconnect={() => {
              void storage.clear();
              queryClient.clear();
              setConnection(null);
            }}
          />
        ) : (
          <Connect onConnect={setConnection} />
        )}
      </QueryClientProvider>
    </SafeAreaProvider>
  );
}
