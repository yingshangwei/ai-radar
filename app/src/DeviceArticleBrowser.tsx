import React, { useEffect, useRef, useState } from "react";
import { AppState, View } from "react-native";
import { WebView } from "react-native-webview";
import { api, APIError } from "./api";
import type { Connection } from "./types";
import {
  CaptureFailure,
  captureNonce,
  extractionScript,
  isWebURL,
  parseCaptureMessage,
} from "./mobileCapture";
import {
  automaticCapture,
  mobilePageURL,
  permittedPage,
  targetPage,
  type DeviceDocument,
} from "./deviceReadingPolicy";
import { loadDeviceSites, updateDeviceSite } from "./deviceReadingStorage";

export type DeviceReadFailure = "verification" | "unavailable" | "permission";
export type DeviceArticleBrowserProps = {
  connection: Connection;
  document: DeviceDocument;
  visible: boolean;
  reloadKey?: number;
  onSaved: (message: string) => void;
  onFailure: (reason: DeviceReadFailure, message: string) => void;
  onProgress?: (message: string, currentURL: string) => void;
};

/** Visits one server-selected article. No credentials or permission state enter the page. */
export default function DeviceArticleBrowser(props: DeviceArticleBrowserProps) {
  const web = useRef<WebView>(null);
  const current = useRef(mobilePageURL(props.document.url));
  const [source, setSource] = useState(current.current);
  const pending = useRef<{ nonce: string; url: string } | undefined>(undefined);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const generation = useRef(0);
  const attempts = useRef(0);
  const live = useRef(true);
  const importing = useRef(false);
  const halted = useRef(false);
  const foreground = useRef(AppState.currentState === "active");
  const verificationUntil = useRef(0);
  const upload = useRef<AbortController | undefined>(undefined);
  const callbacks = useRef(props);
  callbacks.current = props;
  const clear = () => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
    pending.current = undefined;
  };
  const progress = (message: string) =>
    callbacks.current.onProgress?.(message, current.current);
  const fail = (reason: DeviceReadFailure, message: string) => {
    if (!live.current || !foreground.current || halted.current) return;
    if (!props.visible || reason === "permission") halted.current = true;
    callbacks.current.onFailure(reason, message);
  };
  const schedule = (delay: number, expectedGeneration = generation.current) => {
    timers.current.push(
      setTimeout(() => {
        if (
          !live.current ||
          !foreground.current ||
          halted.current ||
          expectedGeneration !== generation.current ||
          importing.current ||
          (attempts.current >= 3 &&
            !(props.visible && Date.now() < verificationUntil.current))
        )
          return;
        if (
          !permittedPage(current.current, props.document.domain) ||
          !targetPage(current.current, props.document.url)
        ) {
          progress("当前网页已跳转。返回目标文章后会自动读取。");
          if (!props.visible)
            fail(
              "unavailable",
              "网页跳转后与目标文章不一致，已跳过并安排稍后重试。",
            );
          return;
        }
        attempts.current++;
        pending.current = { nonce: captureNonce(), url: current.current };
        progress("正在自动读取文章…");
        web.current?.injectJavaScript(extractionScript(pending.current.nonce));
        timers.current.push(
          setTimeout(() => {
            if (
              !live.current ||
              !foreground.current ||
              halted.current ||
              expectedGeneration !== generation.current ||
              !pending.current
            )
              return;
            pending.current = undefined;
            if (
              attempts.current < 3 ||
              (props.visible && Date.now() < verificationUntil.current)
            )
              schedule(5000, expectedGeneration);
            else fail("unavailable", "网页正文暂未就绪，稍后会自动重试。");
          }, 12000),
        );
      }, delay),
    );
  };
  useEffect(() => {
    live.current = true;
    const subscription = AppState.addEventListener("change", (state) => {
      const wasForeground = foreground.current;
      foreground.current = state === "active";
      if (!foreground.current) {
        generation.current++;
        clear();
        upload.current?.abort();
        web.current?.stopLoading();
      } else if (!wasForeground && !halted.current) {
        attempts.current = 0;
        schedule(2000);
      }
    });
    return () => {
      live.current = false;
      subscription.remove();
      upload.current?.abort();
      clear();
    };
  }, []);
  useEffect(() => {
    if (props.reloadKey && foreground.current) {
      clear();
      attempts.current = 0;
      verificationUntil.current = 0;
      web.current?.reload();
    }
  }, [props.reloadKey]);
  useEffect(() => {
    if (props.visible) return;
    const deadline = setTimeout(() => {
      if (foreground.current && !importing.current) {
        clear();
        fail("unavailable", "手机网页加载超时，将在稍后重试。");
      }
    }, 65000);
    return () => clearTimeout(deadline);
  }, []);
  if (!isWebURL(source)) return <View />;
  return (
    <WebView
      ref={web}
      source={{ uri: source }}
      style={{ flex: 1 }}
      originWhitelist={["*"]}
      onShouldStartLoadWithRequest={(request) => {
        if (!foreground.current || !isWebURL(request.url)) return false;
        if (new URL(request.url).protocol !== "https:") {
          if (request.isTopFrame !== false)
            setSource(mobilePageURL(request.url));
          return false;
        }
        if (
          !props.visible &&
          !permittedPage(request.url, props.document.domain) &&
          request.isTopFrame !== false
        ) {
          fail("unavailable", "网页跳转到未许可网站，已跳过并安排稍后重试。");
          return false;
        }
        return true;
      }}
      javaScriptEnabled
      domStorageEnabled
      sharedCookiesEnabled={false}
      thirdPartyCookiesEnabled={false}
      allowFileAccess={false}
      allowFileAccessFromFileURLs={false}
      allowUniversalAccessFromFileURLs={false}
      mixedContentMode="never"
      javaScriptCanOpenWindowsAutomatically={false}
      setSupportMultipleWindows
      geolocationEnabled={false}
      mediaCapturePermissionGrantType="deny"
      onOpenWindow={({ nativeEvent }) => {
        if (
          foreground.current &&
          props.visible &&
          isWebURL(nativeEvent.targetUrl)
        )
          setSource(mobilePageURL(nativeEvent.targetUrl));
      }}
      onLoadStart={() => {
        clear();
        generation.current++;
        attempts.current = 0;
        verificationUntil.current = 0;
        progress("正在打开文章，登录或验证完成后会自动继续。");
      }}
      onNavigationStateChange={(state) => {
        if (isWebURL(state.url)) {
          const changed = current.current !== state.url;
          current.current = state.url;
          // SPA sign-in flows may return through history.pushState without loadEnd.
          if (
            changed &&
            !state.loading &&
            targetPage(state.url, props.document.url)
          ) {
            clear();
            generation.current++;
            attempts.current = 0;
            schedule(2000);
          }
        }
      }}
      onLoadEnd={({ nativeEvent }) => {
        if (isWebURL(nativeEvent.url)) current.current = nativeEvent.url;
        schedule(2000);
      }}
      onError={() => {
        clear();
        fail(
          "unavailable",
          "手机网页暂时无法加载；系统会稍后重试。源站需要支持 HTTPS。",
        );
      }}
      onHttpError={({ nativeEvent }) => {
        if (nativeEvent.statusCode === 429) {
          clear();
          fail("unavailable", "网站限制访问频率，将在冷却后重试。");
        }
      }}
      onRenderProcessGone={() => {
        clear();
        fail("unavailable", "手机浏览器进程已退出，稍后会自动重试。");
      }}
      onContentProcessDidTerminate={() => {
        clear();
        fail("unavailable", "手机浏览器进程已退出，稍后会自动重试。");
      }}
      onMessage={(event) => {
        if (
          !live.current ||
          !foreground.current ||
          halted.current ||
          importing.current
        )
          return;
        let article;
        try {
          article = parseCaptureMessage(
            event.nativeEvent.data,
            pending.current,
            event.nativeEvent.url,
          );
          if (!article) return;
          clear();
        } catch (e) {
          clear();
          if (e instanceof CaptureFailure && e.reason === "verification") {
            fail("verification", e.message);
            if (props.visible) {
              if (!verificationUntil.current)
                verificationUntil.current = Date.now() + 20 * 60 * 1000;
              if (Date.now() < verificationUntil.current) schedule(5000);
              else
                fail(
                  "unavailable",
                  "等待网站验证已超过 20 分钟，可重新打开此网站继续。",
                );
            }
          } else if (
            attempts.current < 3 ||
            (props.visible && Date.now() < verificationUntil.current)
          )
            schedule(5000);
          else fail("unavailable", "网页正文暂未就绪，稍后会自动重试。");
          return;
        }
        const captured = article;
        if (
          !permittedPage(captured.url, props.document.domain) ||
          !targetPage(captured.url, props.document.url)
        )
          return;
        importing.current = true;
        const captureGeneration = generation.current;
        void (async () => {
          try {
            const grants = await loadDeviceSites(props.connection.url);
            if (
              !live.current ||
              !foreground.current ||
              captureGeneration !== generation.current
            )
              return;
            const payload = automaticCapture(props.document, captured, grants);
            if (!payload) {
              fail("permission", "手机自动读取已暂停。");
              return;
            }
            progress("正在保存正文，中文解读会自动生成…");
            const controller = new AbortController();
            upload.current = controller;
            const result = await api<{ ready: boolean; message: string }>(
              props.connection,
              "/v1/browser/mobile-import",
              {
                method: "POST",
                body: JSON.stringify(payload),
                signal: controller.signal,
              },
              60000,
            );
            if (
              !live.current ||
              !foreground.current ||
              controller.signal.aborted
            )
              return;
            if (!result.ready)
              throw new Error(result.message || "正文未保存。");
            await updateDeviceSite(
              props.connection.url,
              props.document.domain,
              { needsVerification: false },
            );
            if (live.current)
              callbacks.current.onSaved(
                result.message || "正文已自动保存，正在生成中文解读。",
              );
          } catch (e) {
            if (
              !live.current ||
              !foreground.current ||
              captureGeneration !== generation.current ||
              upload.current?.signal.aborted
            )
              return;
            const verification =
              e instanceof APIError &&
              e.status === 422 &&
              /登录或验证页面|完成验证/.test(e.message);
            fail(
              verification ? "verification" : "unavailable",
              e instanceof Error ? e.message : "正文保存失败，稍后会自动重试。",
            );
          } finally {
            importing.current = false;
            upload.current = undefined;
          }
        })();
      }}
    />
  );
}
