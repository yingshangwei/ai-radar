import React from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import Feather from "@expo/vector-icons/Feather";
import { C } from "./theme";
import {
  articleContent,
  authorInitials,
  publicationLabel,
} from "./articlePresentation";
import type { Article } from "./types";

export function ArticleByline({
  article: a,
  detail = false,
}: {
  article: Article;
  detail?: boolean;
}) {
  return (
    <View style={styles.byline}>
      <View style={styles.avatar} accessibilityElementsHidden>
        <Text style={styles.initials}>{authorInitials(a.author)}</Text>
      </View>
      <View style={{ flex: 1, minWidth: 0 }}>
        <View style={styles.identity}>
          <Text numberOfLines={1} style={styles.author}>
            {a.author}
          </Text>
          {a.priority && (
            <View style={styles.priorityDot} accessibilityLabel="重点关注" />
          )}
        </View>
        <Text numberOfLines={1} style={styles.secondary}>
          {a.handle ? `@${a.handle.replace(/^@/, "")} · ` : ""}
          {
            { x: "X", facebook: "Facebook", rss: "官方订阅", web: "网页" }[
              a.platform
            ]
          }
        </Text>
        {detail && (
          <Text style={styles.secondary}>
            {publicationLabel(
              a.published_at,
              a.published_precision === "date",
              true,
            )}
            {a.published_precision === "date" ? " · 来源仅公布日期" : ""}
          </Text>
        )}
      </View>
      {!detail && (
        <Text style={styles.time}>
          {publicationLabel(a.published_at, a.published_precision === "date")}
        </Text>
      )}
    </View>
  );
}

export function ReplyContext({
  article,
  onOpen,
}: {
  article: Article;
  onOpen?: (url: string) => void;
}) {
  const reply = article.social?.reply_to;
  if (!reply) return null;
  const identity =
    reply.author ||
    (reply.handle ? `@${reply.handle.replace(/^@/, "")}` : "一条发言");
  const inner = (
    <>
      <Feather name="corner-up-left" size={13} color={C.green} />
      <Text numberOfLines={2} style={styles.replyText}>
        回复 <Text style={{ fontWeight: "600" }}>{identity}</Text>
      </Text>
      {onOpen && <Feather name="external-link" size={12} color={C.green} />}
    </>
  );
  return onOpen && /^https?:\/\//i.test(reply.url) ? (
    <Pressable
      accessibilityRole="link"
      accessibilityLabel={`查看回复对象：${identity}`}
      onPress={() => onOpen(reply.url)}
      style={styles.reply}
    >
      {inner}
    </Pressable>
  ) : (
    <View style={styles.reply}>{inner}</View>
  );
}

export function ArticleBody({
  article,
  original = false,
  preview = false,
}: {
  article: Article;
  original?: boolean;
  preview?: boolean;
}) {
  const content = articleContent(article, original);
  return (
    <View style={{ gap: preview ? 13 : 22 }}>
      {!!content.body && (
        <Text
          selectable={!preview}
          numberOfLines={preview ? 3 : undefined}
          style={preview ? styles.preview : styles.body}
        >
          {content.body}
        </Text>
      )}
      {content.quotes.slice(0, preview ? 1 : undefined).map((quote, i) => (
        <View key={i} style={[styles.quote, !preview && styles.quoteDetail]}>
          <View style={styles.quoteLabel}>
            <Feather name="message-square" size={12} color={C.green} />
            <Text style={styles.quoteType}>引用</Text>
            <Text style={styles.quoteAuthor} numberOfLines={1}>
              {quote.identity}
            </Text>
          </View>
          {!!quote.publishedAt && (
            <Text style={styles.quoteTime}>
              {publicationLabel(
                quote.publishedAt,
                !quote.publishedAt.includes("T"),
              )}
            </Text>
          )}
          <Text
            selectable={!preview}
            numberOfLines={preview ? 2 : undefined}
            style={[
              styles.quoteBody,
              !preview && { fontSize: 15, lineHeight: 26 },
              quote.unavailable && { color: C.muted },
            ]}
          >
            {quote.unavailable
              ? `原帖暂不可用，未取得正文。${quote.text ? `\n\n${quote.text}` : ""}`
              : quote.text || "未取得正文。"}
          </Text>
        </View>
      ))}
      {preview && content.quotes.length > 1 && (
        <Text style={styles.secondary}>
          另有 {content.quotes.length - 1} 条引用 · 详情查看
        </Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  byline: { flexDirection: "row", alignItems: "center", gap: 10 },
  avatar: {
    width: 36,
    height: 36,
    borderRadius: 18,
    backgroundColor: "#E7EBE1",
    alignItems: "center",
    justifyContent: "center",
  },
  initials: { color: C.green, fontWeight: "700", fontSize: 12 },
  identity: { flexDirection: "row", alignItems: "center", gap: 6 },
  author: {
    fontSize: 14,
    lineHeight: 21,
    fontWeight: "600",
    color: C.ink,
    flexShrink: 1,
  },
  secondary: { color: C.muted, fontSize: 11, lineHeight: 18 },
  time: {
    color: C.muted,
    fontSize: 11,
    lineHeight: 18,
    marginLeft: 4,
    alignSelf: "flex-start",
    marginTop: 1,
  },
  priorityDot: {
    height: 5,
    width: 5,
    borderRadius: 3,
    backgroundColor: C.accent,
  },
  reply: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    marginTop: 13,
    alignSelf: "flex-start",
    paddingVertical: 4,
  },
  replyText: { color: C.green, fontSize: 12, lineHeight: 19, flexShrink: 1 },
  preview: { fontSize: 14, lineHeight: 24, color: "#686F65" },
  body: { fontSize: 16, lineHeight: 29, color: C.ink },
  quote: {
    padding: 14,
    backgroundColor: "#EEF0E8",
    borderRadius: 12,
    borderWidth: 1,
    borderColor: "#E0E4D9",
  },
  quoteDetail: { padding: 18, borderLeftWidth: 3, borderLeftColor: "#A7B49A" },
  quoteLabel: { flexDirection: "row", alignItems: "center", gap: 6 },
  quoteType: { fontSize: 10, fontWeight: "600", color: C.green },
  quoteAuthor: {
    fontSize: 12,
    lineHeight: 19,
    color: C.ink,
    fontWeight: "600",
    flex: 1,
  },
  quoteTime: { fontSize: 10, lineHeight: 17, color: C.muted, marginTop: 2 },
  quoteBody: { color: "#626A5C", fontSize: 13, lineHeight: 22, marginTop: 8 },
});
