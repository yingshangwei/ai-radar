import React, { useState } from "react";
import {
  ActivityIndicator,
  Linking,
  Pressable,
  ScrollView,
  Text,
  TextInput,
  View,
} from "react-native";
import { api, APIError } from "./api";
import { C, s } from "./theme";
import type { Connection } from "./types";
import {
  isWebURL,
  MAX_CAPTURE_CHARS,
  type MobileArticle,
} from "./mobileCapture";

export type MobileReaderProps = {
  connection: Connection;
  document: { document_id: string; url: string; domain: string };
  onDone: (message: string) => void;
};

export async function openSystemArticle(url: string): Promise<void> {
  if (!isWebURL(url)) throw new Error("此链接不能在浏览器中打开。");
  await Linking.openURL(url);
}

export default function MobileArticleReview({
  connection,
  document,
  onDone,
  article,
  onBack,
}: MobileReaderProps & { article?: MobileArticle; onBack?: () => void }) {
  const [title, setTitle] = useState(article?.title || "");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const manual = !article;
  const body = article?.text || text.trim();
  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const result = await api<{ ready: boolean; message: string }>(
        connection,
        "/v1/browser/mobile-import",
        {
          method: "POST",
          body: JSON.stringify({
            document_id: document.document_id,
            url: article?.url || document.url,
            title: title.trim() || `${document.domain} 文章正文`,
            text: body,
            links: article?.links || [],
            partial: manual || article?.partial || false,
            method: manual ? "manual" : "mobile_browser",
          }),
        },
        60000,
      );
      if (!result.ready)
        throw new Error(result.message || "正文尚未保存，请稍后重试。");
      onDone(result.message || "正文已保存，正在安排中文翻译与解读。");
    } catch (e) {
      setError(
        e instanceof APIError && e.status === 403
          ? "需要管理权限。请返回网页采集中心，验证并保存管理令牌后重试。"
          : e instanceof Error
            ? e.message
            : "提交未完成，请稍后重试。",
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <View style={{ flex: 1 }}>
      <ScrollView
        contentContainerStyle={{ padding: 20, gap: 14 }}
        keyboardShouldPersistTaps="handled"
      >
        <Text style={s.sectionTitle}>
          {manual ? "粘贴文章正文" : "确认要提交的正文"}
        </Text>
        <Text style={s.muted}>
          {manual
            ? "在系统浏览器中打开原文，完成登录或验证后复制可见正文，再回到这里粘贴。此方式会标记为手动选取的内容。"
            : "以下内容将保存到雷达，并用于中文翻译和解读。请确认这是目标文章，且没有混入私人信息。"}
        </Text>
        <Text selectable style={[s.muted, { color: C.green }]}>
          {article?.url || document.url}
        </Text>
        {manual ? (
          <>
            <Pressable
              style={s.smallButton}
              onPress={() =>
                void openSystemArticle(document.url).catch(() =>
                  setError("系统浏览器未能打开，请复制上方原文链接。"),
                )
              }
            >
              <Text style={s.body}>用系统浏览器打开原文 ↗</Text>
            </Pressable>
            <TextInput
              style={s.input}
              value={title}
              onChangeText={setTitle}
              maxLength={500}
              placeholder="文章标题（可选）"
              accessibilityLabel="文章标题"
            />
            <TextInput
              style={[s.input, { minHeight: 250, textAlignVertical: "top" }]}
              value={text}
              onChangeText={setText}
              multiline
              maxLength={MAX_CAPTURE_CHARS}
              placeholder="长按此处，粘贴你刚读到的文章正文…"
              accessibilityLabel="粘贴文章正文"
              autoCorrect={false}
            />
            <Text style={s.muted}>
              {body.length.toLocaleString()} 字符 · 至少 80 字符
            </Text>
          </>
        ) : (
          <>
            <Text style={s.cardTitle}>{article.title || "文章正文"}</Text>
            <Text style={s.muted}>
              {body.length.toLocaleString()} 字符 · {article.links.length}{" "}
              个正文链接{article.partial ? " · 部分正文" : ""}
            </Text>
            <Text selectable style={s.body}>
              {body}
            </Text>
          </>
        )}
        {!!error && (
          <Text accessibilityRole="alert" style={[s.body, { color: C.accent }]}>
            {error}
          </Text>
        )}
      </ScrollView>
      <View
        style={{
          padding: 16,
          gap: 10,
          borderTopWidth: 1,
          borderColor: C.line,
          backgroundColor: C.paper,
        }}
      >
        <Pressable
          accessibilityRole="button"
          disabled={busy || body.length < 80}
          style={[s.button, { opacity: busy || body.length < 80 ? 0.5 : 1 }]}
          onPress={() => void submit()}
        >
          {busy ? (
            <ActivityIndicator color={C.paper} />
          ) : (
            <Text style={s.buttonText}>确认提交正文</Text>
          )}
        </Pressable>
        {onBack && (
          <Pressable
            accessibilityRole="button"
            disabled={busy}
            style={{ alignItems: "center", padding: 5 }}
            onPress={onBack}
          >
            <Text style={s.muted}>返回网页</Text>
          </Pressable>
        )}
      </View>
    </View>
  );
}
