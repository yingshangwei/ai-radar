import React, { useEffect, useRef, useState } from "react";
import { AppState, View, useWindowDimensions } from "react-native";
import { api } from "./api";
import type { Connection } from "./types";
import DeviceArticleBrowser, {
  type DeviceReadFailure,
} from "./DeviceArticleBrowser";
import {
  automaticDomains,
  canRunDeviceReading,
  deferredDocumentIds,
  eligibleDeviceDocument,
  siteHost,
  type DeviceDocument,
} from "./deviceReadingPolicy";
import {
  deferDeviceDocument,
  deviceAdminConnection,
  loadDeviceDeferrals,
  loadDeviceRotation,
  loadDeviceSites,
  saveDeviceRotation,
  subscribeDeviceReading,
  updateDeviceSite,
} from "./deviceReadingStorage";

type Result = {
  saved?: boolean;
  reason?: DeviceReadFailure;
  cancelled?: boolean;
};
type Work = {
  id: string;
  document: DeviceDocument;
  connection: Connection;
  finish: (result: Result) => void;
};

export type DeviceReadingProps = {
  connection: Connection;
  paused: boolean;
  onStatus: (message: string) => void;
  onComplete: () => void;
};

export default function DeviceReading(props: DeviceReadingProps) {
  const { width, height } = useWindowDimensions();
  const [active, setActive] = useState(AppState.currentState || "inactive");
  const [tick, setTick] = useState(0);
  const [work, setWork] = useState<Work>();
  const current = useRef<Work | undefined>(undefined);
  const working = useRef(false);
  const callbacks = useRef(props);
  callbacks.current = props;
  useEffect(() => {
    const app = AppState.addEventListener("change", setActive);
    const timer = setInterval(
      () => {
        if (AppState.currentState === "active" && !working.current)
          setTick((value) => value + 1);
      },
      5 * 60 * 1000,
    );
    const unsubscribe = subscribeDeviceReading(() => {
      if (!working.current) setTick((value) => value + 1);
      else
        void loadDeviceSites(callbacks.current.connection.url).then(
          (permissions) => {
            if (
              current.current &&
              !permissions[siteHost(current.current.document.domain)]?.allowed
            )
              setTick((value) => value + 1);
          },
        );
    });
    return () => {
      app.remove();
      clearInterval(timer);
      unsubscribe();
    };
  }, []);
  useEffect(() => {
    if (!canRunDeviceReading(active, props.paused, !!props.connection.demo))
      return;
    let cancelled = false;
    working.current = true;
    void (async () => {
      let saved = 0;
      const verification = new Set<string>();
      try {
        const permissions = await loadDeviceSites(props.connection.url);
        const domains = automaticDomains(
          permissions,
          await loadDeviceRotation(props.connection.url),
        );
        Object.entries(permissions).forEach(([domain, value]) => {
          if (value.allowed && value.needsVerification)
            verification.add(domain);
        });
        if (!domains.length || cancelled) return;
        const connection = await deviceAdminConnection(props.connection);
        const deferred = await loadDeviceDeferrals(props.connection.url);
        const excluded = deferredDocumentIds(deferred, Date.now());
        const response = await api<DeviceDocument[]>(
          connection,
          `/v1/browser/mobile-queue?domains=${encodeURIComponent(domains.join(","))}&limit=3&exclude_document_ids=${encodeURIComponent(excluded.join(","))}`,
        );
        if (!Array.isArray(response) || cancelled) return;
        // Advance only after a successful queue read. Persist per server so short
        // foreground sessions also reach sites beyond the first thirty grants.
        await saveDeviceRotation(
          props.connection.url,
          domains[domains.length - 1]!,
        );
        if (cancelled) return;
        const queue = response
          .filter(
            (doc) =>
              doc &&
              typeof doc.url === "string" &&
              typeof doc.domain === "string" &&
              eligibleDeviceDocument(doc, domains, deferred, Date.now()),
          )
          .slice(0, 3);
        if (queue.length)
          callbacks.current.onStatus("手机正在自动补采已授权网站的文章…");
        for (const document of queue) {
          if (cancelled) break;
          const latest = await loadDeviceSites(props.connection.url);
          if (
            !latest[siteHost(document.domain)]?.allowed ||
            latest[siteHost(document.domain)]?.needsVerification ||
            cancelled
          )
            continue;
          const result = await new Promise<Result>((resolve) => {
            const next: Work = {
              id: `${tick}-${document.document_id}`,
              document,
              connection,
              finish: resolve,
            };
            current.current = next;
            setWork(next);
          });
          current.current = undefined;
          setWork(undefined);
          if (result.cancelled || cancelled) break;
          if (result.saved) saved++;
          else if (result.reason === "verification") {
            verification.add(document.domain);
            await updateDeviceSite(props.connection.url, document.domain, {
              needsVerification: true,
            });
          } else if (result.reason === "unavailable")
            await deferDeviceDocument(
              props.connection.url,
              document.document_id,
            );
        }
      } catch {
        // Temporary API/network errors are retried on the next bounded foreground run.
      } finally {
        if (!cancelled) {
          working.current = false;
          if (verification.size)
            callbacks.current.onStatus(
              `${Array.from(verification).slice(0, 2).join("、")}：手机访问需要重新验证。`,
            );
          else if (saved)
            callbacks.current.onStatus(
              `手机已自动补采 ${saved} 篇文章，中文解读正在生成。`,
            );
          else callbacks.current.onStatus("");
          if (saved) callbacks.current.onComplete();
        }
      }
    })();
    return () => {
      cancelled = true;
      working.current = false;
      current.current?.finish({ cancelled: true });
      current.current = undefined;
      setWork(undefined);
    };
  }, [
    tick,
    active,
    props.paused,
    props.connection.url,
    props.connection.token,
    props.connection.demo,
  ]);
  if (
    !work ||
    !canRunDeviceReading(active, props.paused, !!props.connection.demo)
  )
    return null;
  return (
    <View
      pointerEvents="none"
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
      collapsable={false}
      style={{
        position: "absolute",
        left: -width - 32,
        top: 0,
        width,
        height: Math.max(height, 700),
        opacity: 0.01,
      }}
    >
      <DeviceArticleBrowser
        key={work.id}
        connection={work.connection}
        document={work.document}
        visible={false}
        onSaved={() => work.finish({ saved: true })}
        onFailure={(reason) => work.finish({ reason })}
      />
    </View>
  );
}
