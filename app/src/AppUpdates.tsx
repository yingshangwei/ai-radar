import React, { useEffect, useState } from "react";
import { AppState, Pressable, Text, View } from "react-native";
import * as Updates from "expo-updates";
import { C, s } from "./theme";
import { updateCoordinator, updateMessage } from "./updateState";
import { updateRevision } from "./updateRevision";

const check = updateCoordinator(async () => {
  if (!Updates.isEnabled) return;
  const result = await Updates.checkForUpdateAsync();
  if (result.isAvailable || result.isRollBackToEmbedded)
    await Updates.fetchUpdateAsync();
});

export function UpdateLifecycle() {
  useEffect(() => {
    if (!Updates.isEnabled) return;
    // Native ON_LOAD handles cold start, including startup error recovery.
    let previous = AppState.currentState;
    const listener = AppState.addEventListener("change", (next) => {
      if (next === "active" && previous !== "active")
        void check().catch(() => {});
      previous = next;
    });
    return () => listener.remove();
  }, []);
  return null;
}

export default function AppUpdates() {
  const u = Updates.useUpdates();
  const [manualError, setManualError] = useState(false);
  const busy =
    u.isChecking ||
    u.isDownloading ||
    u.isStartupProcedureRunning ||
    u.isRestarting;
  const status = updateMessage({
    enabled: Updates.isEnabled,
    pending: u.isUpdatePending,
    checking: u.isChecking,
    downloading: u.isDownloading,
    failed: !!u.checkError || !!u.downloadError || manualError,
    checked: !!u.lastCheckForUpdateTimeSinceRestart,
    emergency: u.currentlyRunning.isEmergencyLaunch,
  });
  return (
    <View style={[s.note, { marginBottom: 20 }]}>
      <View style={s.spread}>
        <Text style={[s.body, { fontWeight: "600" }]}>应用更新</Text>
        <Text style={[s.muted, { fontSize: 12 }]}>{updateRevision}</Text>
      </View>
      <Text
        accessibilityLiveRegion="polite"
        style={[s.muted, { marginTop: 8 }]}
      >
        {status}
      </Text>
      {Updates.isEnabled && (
        <Pressable
          accessibilityRole="button"
          disabled={busy}
          style={[
            s.smallButton,
            { alignSelf: "flex-start", marginTop: 12, opacity: busy ? 0.5 : 1 },
          ]}
          onPress={() => {
            setManualError(false);
            const action = u.isUpdatePending
              ? Updates.reloadAsync()
              : check(true);
            void action.catch(() => setManualError(true));
          }}
        >
          <Text style={[s.body, { fontSize: 12, color: C.green }]}>
            {u.isUpdatePending
              ? "立即应用更新"
              : busy
                ? "正在处理"
                : "检查更新"}
          </Text>
        </Pressable>
      )}
    </View>
  );
}
