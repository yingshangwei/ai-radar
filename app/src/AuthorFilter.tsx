import React, { useState } from "react";
import {
  ActivityIndicator,
  FlatList,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
  useWindowDimensions,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import Feather from "@expo/vector-icons/Feather";
import { useQuery } from "@tanstack/react-query";
import { cached } from "./api";
import {
  authorParams,
  demoAuthorOptions,
  matchingAuthors,
} from "./authorSelection";
import type { AuthorOption } from "./authorSelection";
import type { Article, Connection } from "./types";
import { C } from "./theme";
import { authorInitials } from "./articlePresentation";

const platforms = {
  x: "X",
  facebook: "Facebook",
  rss: "官方订阅",
  web: "网页",
};

export default function AuthorFilter({
  connection,
  params,
  selected,
  onChange,
  demoItems,
}: {
  connection: Connection;
  params: URLSearchParams;
  selected: AuthorOption | null;
  onChange: (author: AuthorOption | null) => void;
  demoItems: Article[];
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const { height } = useWindowDimensions();
  const scope = authorParams(params);
  const query = useQuery({
    queryKey: [
      connection.url,
      connection.demo ? "demo" : "live",
      "article-authors",
      scope,
    ],
    enabled: open,
    staleTime: 0,
    queryFn: ({ signal }) =>
      connection.demo
        ? Promise.resolve({
            data: {
              items: demoAuthorOptions(demoItems),
              total: demoItems.length,
            },
            offline: false,
          })
        : cached<{ items: AuthorOption[]; total: number }>(
            connection,
            `/v1/article-authors?${scope}`,
            signal,
          ),
  });
  const items = matchingAuthors(query.data?.data.items || [], search);
  const choose = (author: AuthorOption | null) => {
    onChange(author);
    setOpen(false);
  };
  return (
    <>
      <View style={styles.control}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={
            selected ? `更换发言人，当前 ${selected.name}` : "按发言人筛选"
          }
          onPress={() => {
            setSearch("");
            setOpen(true);
          }}
          style={styles.trigger}
        >
          <View
            style={[styles.symbol, selected && { backgroundColor: C.pale }]}
          >
            <Feather
              name="users"
              size={17}
              color={selected ? C.accent : C.green}
            />
          </View>
          <View style={{ flex: 1, minWidth: 0 }}>
            <Text style={styles.eyebrow}>发言人</Text>
            <Text
              numberOfLines={1}
              style={[styles.selected, selected && { color: C.accent }]}
            >
              {selected ? selected.name : "全部发言人"}
            </Text>
          </View>
          {selected && (
            <Text numberOfLines={1} style={styles.platform}>
              {platforms[selected.platform]}
            </Text>
          )}
          <Feather name="chevron-down" size={16} color={C.muted} />
        </Pressable>
        {selected && (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="清除发言人筛选"
            onPress={() => onChange(null)}
            style={styles.clear}
          >
            <Feather name="x" size={17} color={C.muted} />
          </Pressable>
        )}
      </View>
      <Modal
        visible={open}
        transparent
        animationType="slide"
        onRequestClose={() => setOpen(false)}
      >
        <KeyboardAvoidingView
          style={styles.overlay}
          behavior={Platform.OS === "ios" ? "padding" : "height"}
        >
          <Pressable
            accessibilityLabel="关闭发言人筛选"
            accessibilityRole="button"
            style={StyleSheet.absoluteFill}
            onPress={() => setOpen(false)}
          />
          <SafeAreaView
            edges={["bottom"]}
            style={[styles.sheet, { height: Math.min(660, height * 0.82) }]}
          >
            <View style={styles.grabber} />
            <View style={styles.heading}>
              <View style={{ flex: 1 }}>
                <Text style={styles.title}>按发言人筛选</Text>
                <Text style={styles.subtitle}>
                  当前条件下有信息的账号与作者
                </Text>
              </View>
              <Pressable
                accessibilityLabel="关闭筛选窗口"
                accessibilityRole="button"
                onPress={() => setOpen(false)}
                style={styles.clear}
              >
                <Feather name="x" size={21} color={C.ink} />
              </Pressable>
            </View>
            <View style={styles.search}>
              <Feather name="search" size={17} color={C.muted} />
              <TextInput
                accessibilityLabel="搜索发言人"
                value={search}
                onChangeText={setSearch}
                placeholder="搜索姓名或 @账号"
                placeholderTextColor={C.muted}
                autoCorrect={false}
                autoCapitalize="none"
                style={styles.input}
              />
              {!!search && (
                <Pressable
                  accessibilityLabel="清空发言人搜索"
                  accessibilityRole="button"
                  hitSlop={10}
                  onPress={() => setSearch("")}
                >
                  <Feather name="x-circle" size={17} color={C.muted} />
                </Pressable>
              )}
            </View>
            {query.data?.offline && (
              <Text style={styles.hint}>离线列表 · 数量以上次连接时为准</Text>
            )}
            <Pressable
              accessibilityRole="button"
              accessibilityState={{ selected: !selected }}
              onPress={() => choose(null)}
              style={styles.all}
            >
              <Text style={[styles.name, { flex: 1 }]}>全部发言人</Text>
              {query.data && (
                <Text style={styles.count}>{query.data.data.total} 条</Text>
              )}
              {!selected && <Feather name="check" size={18} color={C.accent} />}
            </Pressable>
            <FlatList
              data={items}
              keyExtractor={(a) => a.key}
              keyboardShouldPersistTaps="handled"
              contentContainerStyle={{ paddingBottom: 16 }}
              style={{ flex: 1 }}
              renderItem={({ item }) => (
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel={`只看 ${item.name}${item.handle ? ` @${item.handle}` : ""}，${platforms[item.platform]}，${item.count} 条`}
                  accessibilityState={{ selected: selected?.key === item.key }}
                  onPress={() => choose(item)}
                  style={[
                    styles.person,
                    selected?.key === item.key && { backgroundColor: C.pale },
                  ]}
                >
                  <View style={styles.avatar}>
                    <Text style={styles.initials}>
                      {authorInitials(item.name)}
                    </Text>
                  </View>
                  <View style={{ flex: 1, minWidth: 0 }}>
                    <Text numberOfLines={1} style={styles.name}>
                      {item.name}
                    </Text>
                    <Text numberOfLines={1} style={styles.subtitle}>
                      {item.handle ? `@${item.handle} · ` : ""}
                      {platforms[item.platform]}
                    </Text>
                  </View>
                  <Text style={styles.count}>{item.count} 条</Text>
                  {selected?.key === item.key && (
                    <Feather name="check" size={17} color={C.accent} />
                  )}
                </Pressable>
              )}
              ListEmptyComponent={
                query.isLoading ? (
                  <ActivityIndicator
                    color={C.accent}
                    style={{ marginTop: 34 }}
                  />
                ) : query.isError ? (
                  <View style={{ padding: 24, gap: 12 }}>
                    <Text style={styles.hint}>发言人列表暂时无法加载。</Text>
                    <Pressable
                      accessibilityRole="button"
                      onPress={() => void query.refetch()}
                      style={styles.retry}
                    >
                      <Text style={{ color: C.accent }}>重试</Text>
                    </Pressable>
                  </View>
                ) : (
                  <Text style={[styles.hint, { padding: 30 }]}>
                    {search
                      ? "没有找到匹配的发言人"
                      : "当前条件下暂无消息，可调整主题或来源"}
                  </Text>
                )
              }
            />
          </SafeAreaView>
        </KeyboardAvoidingView>
      </Modal>
    </>
  );
}

const styles = StyleSheet.create({
  control: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: C.paper,
    borderColor: C.line,
    borderWidth: 1,
    borderRadius: 14,
    marginBottom: 16,
  },
  trigger: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    padding: 12,
    minHeight: 62,
  },
  symbol: {
    width: 36,
    height: 36,
    borderRadius: 11,
    backgroundColor: "#EAECE2",
    alignItems: "center",
    justifyContent: "center",
  },
  eyebrow: { color: C.muted, fontSize: 10, marginBottom: 3 },
  selected: { color: C.ink, fontSize: 14, fontWeight: "600" },
  platform: { color: C.muted, fontSize: 11, maxWidth: 72 },
  clear: {
    width: 44,
    height: 44,
    alignItems: "center",
    justifyContent: "center",
  },
  overlay: {
    flex: 1,
    justifyContent: "flex-end",
    alignItems: "center",
    backgroundColor: "rgba(20,28,22,0.36)",
  },
  sheet: {
    backgroundColor: C.paper,
    width: "100%",
    maxWidth: 600,
    borderTopLeftRadius: 26,
    borderTopRightRadius: 26,
    paddingHorizontal: 20,
  },
  grabber: {
    width: 36,
    height: 4,
    backgroundColor: C.line,
    borderRadius: 4,
    alignSelf: "center",
    marginTop: 10,
    marginBottom: 8,
  },
  heading: { flexDirection: "row", alignItems: "center", paddingVertical: 10 },
  title: { fontSize: 22, fontWeight: "700", color: C.ink, marginBottom: 6 },
  subtitle: { color: C.muted, fontSize: 12, lineHeight: 20 },
  search: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    backgroundColor: C.bg,
    borderRadius: 12,
    paddingHorizontal: 14,
    marginVertical: 12,
  },
  input: { flex: 1, color: C.ink, paddingVertical: 14, fontSize: 14 },
  all: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingVertical: 18,
    paddingHorizontal: 12,
    borderBottomWidth: 1,
    borderColor: C.line,
    marginBottom: 6,
  },
  person: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    padding: 12,
    borderRadius: 12,
    minHeight: 72,
  },
  avatar: {
    width: 40,
    height: 40,
    borderRadius: 13,
    backgroundColor: "#EAECE2",
    alignItems: "center",
    justifyContent: "center",
  },
  initials: { fontSize: 13, fontWeight: "700", color: C.green },
  name: { color: C.ink, fontSize: 14, fontWeight: "600", lineHeight: 23 },
  count: { fontSize: 12, color: C.muted, fontVariant: ["tabular-nums"] },
  hint: { color: C.muted, textAlign: "center", fontSize: 12, lineHeight: 21 },
  retry: { alignItems: "center", padding: 10 },
});
