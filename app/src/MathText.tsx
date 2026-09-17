import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  Platform,
  Linking,
  StyleSheet,
  Text,
  View,
  useWindowDimensions,
  type StyleProp,
  type TextStyle,
  type ViewStyle,
} from "react-native";
import { WebView } from "react-native-webview";
import {
  mathDocument,
  mathSpans,
  richPreview,
  richPlainText,
} from "./mathDocument";

export default function MathText({
  text,
  style,
  preview = false,
  lines = 3,
}: {
  text: string;
  style?: StyleProp<TextStyle>;
  preview?: boolean;
  lines?: number;
}) {
  const size = useWindowDimensions();
  const flat = StyleSheet.flatten(style) || {};
  const hasMath = useMemo(
    () =>
      mathSpans(text).length > 0 ||
      /[\n*`#>[\]~]|^\d+\./.test(text) ||
      text.length > 400,
    [text],
  );
  const nonce = useMemo(
    () => Math.random().toString(36).slice(2) + Date.now().toString(36),
    [text],
  );
  const [height, setHeight] = useState(36);
  const [failed, setFailed] = useState(false);
  const ready = useRef(false);
  const frame = useRef<HTMLIFrameElement>(null);
  const html = useMemo(
    () =>
      hasMath && !preview
        ? mathDocument(text, {
            nonce,
            fontSize: (flat.fontSize || 16) * size.fontScale,
            lineHeight: (flat.lineHeight || 28) * size.fontScale,
            fontWeight: String(flat.fontWeight || "normal"),
            color: typeof flat.color === "string" ? flat.color : "#293326",
          })
        : "",
    [
      text,
      nonce,
      hasMath,
      preview,
      flat.fontSize,
      flat.lineHeight,
      flat.color,
      flat.fontWeight,
      size.fontScale,
    ],
  );
  const receive = (message: unknown) => {
    if (!message || typeof message !== "object") return;
    const data = message as {
      type?: string;
      nonce?: string;
      height?: number;
      url?: string;
    };
    if (
      data.type === "rich-link" &&
      data.nonce === nonce &&
      typeof data.url === "string" &&
      /^https?:\/\//i.test(data.url)
    ) {
      void Linking.openURL(data.url).catch(() => {});
      return;
    }
    if (
      data.type === "math-size" &&
      data.nonce === nonce &&
      typeof data.height === "number" &&
      Number.isFinite(data.height) &&
      data.height > 0 &&
      data.height <= 500000
    ) {
      ready.current = true;
      setHeight(Math.ceil(data.height));
    }
  };
  useEffect(() => {
    ready.current = false;
    setFailed(false);
    setHeight(36);
    if (!html) return;
    const timeout = setTimeout(() => {
      if (!ready.current) setFailed(true);
    }, 8000);
    return () => clearTimeout(timeout);
  }, [html]);
  useEffect(() => {
    if (Platform.OS !== "web" || !hasMath || preview) return;
    const listener = (event: MessageEvent) => {
      if (event.source === frame.current?.contentWindow) receive(event.data);
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, [nonce, hasMath, preview]);
  if (!hasMath || preview || failed)
    return (
      <Text
        selectable={!preview}
        numberOfLines={preview ? lines : undefined}
        style={style}
      >
        {preview ? richPreview(text) : failed ? richPlainText(text) : text}
      </Text>
    );
  return (
    <View
      style={[
        style as StyleProp<ViewStyle>,
        { height, minWidth: 0, backgroundColor: "transparent" },
      ]}
    >
      {Platform.OS === "web" ? (
        React.createElement("iframe", {
          ref: frame,
          srcDoc: html,
          title: "排版正文",
          sandbox: "allow-scripts",
          style: {
            width: "100%",
            height,
            border: 0,
            display: "block",
            background: "transparent",
          },
        })
      ) : (
        <WebView
          key={nonce}
          source={{ html }}
          originWhitelist={["about:blank"]}
          javaScriptEnabled
          sharedCookiesEnabled={false}
          thirdPartyCookiesEnabled={false}
          domStorageEnabled={false}
          allowFileAccess={false}
          allowFileAccessFromFileURLs={false}
          allowUniversalAccessFromFileURLs={false}
          mixedContentMode="never"
          scrollEnabled={false}
          nestedScrollEnabled={false}
          textZoom={100}
          setSupportMultipleWindows={false}
          showsVerticalScrollIndicator={false}
          onShouldStartLoadWithRequest={(request) =>
            request.url === "about:blank" || request.url === "about:srcdoc"
          }
          onMessage={(event) => {
            try {
              receive(JSON.parse(event.nativeEvent.data));
            } catch {
              /* Ignore malformed sizing data. */
            }
          }}
          onError={() => setFailed(true)}
          onContentProcessDidTerminate={() => setFailed(true)}
          onRenderProcessGone={() => setFailed(true)}
          style={{ backgroundColor: "transparent" }}
        />
      )}
    </View>
  );
}
