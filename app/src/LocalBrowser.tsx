import React, { useState } from "react";
import { ActivityIndicator, Pressable, Text, View } from "react-native";
import { C, s } from "./theme";
import DeviceArticleBrowser from "./DeviceArticleBrowser";
import { isWebURL } from "./mobileCapture";
import { updateDeviceSite } from "./deviceReadingStorage";
import MobileArticleReview, {
  openSystemArticle,
  type MobileReaderProps,
} from "./MobileArticleReview";

export default function LocalBrowser(props: MobileReaderProps) {
  const [status, setStatus] = useState("正在打开文章…");
  const [currentURL, setCurrentURL] = useState(props.document.url);
  const [error, setError] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [manual, setManual] = useState(false);
  const [reload, setReload] = useState(0);
  if (manual)
    return <MobileArticleReview {...props} onBack={() => setManual(false)} />;
  return (
    <View style={{ flex: 1 }}>
      <View
        style={{
          paddingHorizontal: 18,
          paddingVertical: 10,
          backgroundColor: "#EEEFE6",
        }}
      >
        <Text style={[s.muted, { color: C.green, fontWeight: "700" }]}>
          手机自动读取已开启 · 仅 {props.document.domain}
        </Text>
        <Text numberOfLines={1} style={s.muted}>
          {isWebURL(currentURL) ? new URL(currentURL).host : "来源网页"} ·
          使用手机网络
        </Text>
      </View>
      <DeviceArticleBrowser
        {...props}
        visible
        reloadKey={reload}
        onProgress={(message, url) => {
          setError("");
          setStatus(message);
          setCurrentURL(url);
        }}
        onSaved={props.onDone}
        onFailure={(reason, message) => {
          setError(message);
          if (reason === "verification")
            void updateDeviceSite(props.connection.url, props.document.domain, {
              needsVerification: true,
            });
        }}
      />
      <View
        style={{
          padding: 16,
          gap: 9,
          borderTopWidth: 1,
          borderColor: C.line,
          backgroundColor: C.paper,
        }}
      >
        <View style={{ flexDirection: "row", gap: 10, alignItems: "center" }}>
          {!error && <ActivityIndicator color={C.green} size="small" />}
          <Text
            accessibilityLiveRegion="polite"
            style={[s.muted, { flex: 1, color: error ? C.accent : C.green }]}
          >
            {error || status}
          </Text>
        </View>
        <Text style={s.muted}>
          你只需完成网站登录或验证。文章加载后会自动保存，后续在 App
          前台自动补采。
        </Text>
        <Pressable
          accessibilityRole="button"
          onPress={() => setAdvanced(!advanced)}
          style={{ alignSelf: "flex-end", paddingVertical: 4 }}
        >
          <Text style={s.muted}>
            {advanced ? "收起备用方式" : "高级备用方式"}
          </Text>
        </Pressable>
        {advanced && (
          <>
            <View style={s.spread}>
              <Pressable
                accessibilityRole="button"
                style={{ paddingVertical: 6 }}
                onPress={() => {
                  setError("");
                  setReload(reload + 1);
                }}
              >
                <Text style={s.muted}>重新加载</Text>
              </Pressable>
              <Pressable
                accessibilityRole="button"
                style={{ paddingVertical: 6 }}
                onPress={() =>
                  void openSystemArticle(props.document.url).catch(() =>
                    setError("系统浏览器暂时无法打开。"),
                  )
                }
              >
                <Text style={s.muted}>系统浏览器 ↗</Text>
              </Pressable>
              <Pressable
                accessibilityRole="button"
                style={{ paddingVertical: 6 }}
                onPress={() => setManual(true)}
              >
                <Text style={s.muted}>手动粘贴</Text>
              </Pressable>
            </View>
            <Text style={[s.muted, { fontSize: 10, lineHeight: 15 }]}>
              系统浏览器不会自动回传文章；只有需要时才使用复制粘贴。网站登录状态始终留在手机。
            </Text>
          </>
        )}
      </View>
    </View>
  );
}
