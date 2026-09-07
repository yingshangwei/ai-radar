import React from "react";
import { WebView } from "react-native-webview";

export default function RemoteBrowser({
  url,
  onDone,
}: {
  url: string;
  onDone: () => void;
}) {
  const origin = new URL(url).origin;
  return (
    <WebView
      source={{ uri: url }}
      style={{ flex: 1 }}
      originWhitelist={[origin]}
      javaScriptEnabled
      domStorageEnabled={false}
      sharedCookiesEnabled={false}
      thirdPartyCookiesEnabled={false}
      allowFileAccess={false}
      allowFileAccessFromFileURLs={false}
      allowUniversalAccessFromFileURLs={false}
      mixedContentMode="never"
      setSupportMultipleWindows={false}
      onShouldStartLoadWithRequest={(request) =>
        request.url.startsWith(origin + "/v1/browser/view/")
      }
      onMessage={(event) => {
        try {
          const message = JSON.parse(event.nativeEvent.data);
          if (["complete", "close"].includes(message.type)) onDone();
        } catch (_) {}
      }}
    />
  );
}
