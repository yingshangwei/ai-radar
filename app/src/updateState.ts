export type UpdateState = {
  enabled: boolean;
  pending: boolean;
  checking: boolean;
  downloading: boolean;
  failed: boolean;
  checked: boolean;
  emergency: boolean;
};

export function updateMessage(state: UpdateState): string {
  if (!state.enabled) return "当前构建未启用自动更新";
  if (state.pending) return "更新已就绪，下次启动自动生效";
  if (state.downloading) return "正在下载更新，你可以继续阅读";
  if (state.checking) return "正在检查更新";
  if (state.failed) return "暂时无法获取更新，当前版本可继续使用";
  if (state.emergency) return "已恢复至内置版本，将继续检查修复更新";
  if (state.checked) return "已是当前可用的最新版本";
  return "自动更新已开启，下次启动检查更新";
}

/** Coalesce foreground/manual checks; native code owns download integrity and persistence. */
export function updateCoordinator(
  run: () => Promise<void>,
  now: () => number = Date.now,
) {
  let inFlight: Promise<void> | null = null;
  let lastAttempt = -Infinity;
  return (manual = false) => {
    if (inFlight) return inFlight;
    if (now() - lastAttempt < (manual ? 30_000 : 15 * 60_000))
      return Promise.resolve();
    lastAttempt = now();
    inFlight = Promise.resolve()
      .then(run)
      .finally(() => {
        inFlight = null;
      });
    return inFlight;
  };
}
