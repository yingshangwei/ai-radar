import type { QueryClient } from "@tanstack/react-query";
import type { Article, Status } from "./types";

const CONTENT_QUERIES = new Set([
  "articles",
  "article",
  "digest",
  "editions",
  "watches",
]);
const counts = (value?: Record<string, number>) =>
  Object.entries(value || {}).sort(([a], [b]) => a.localeCompare(b));

/** Status timestamps/messages can change without changing any readable content. */
export function contentRevision(status: Status): string {
  return JSON.stringify([
    status.article_count,
    counts(status.translation?.counts),
    counts(status.translation?.resource_counts),
    status.sources
      .map((source) => [source.id, source.last_success_at || ""])
      .sort(([a], [b]) => a!.localeCompare(b!)),
    status.jobs
      .filter((job) =>
        ["completed", "failed", "needs_attention"].includes(job.status),
      )
      .map((job) => [job.id, job.status, job.finished_at || ""])
      .sort(([a], [b]) => a!.localeCompare(b!)),
  ]);
}

/** One tracker per connection. Refresh content, never the status poll itself. */
export function contentRefreshTracker(client: QueryClient, prefix: string[]) {
  let previous: string | undefined;
  let wasOffline = false;
  return async (snapshot?: { data: Status; offline: boolean }) => {
    if (!snapshot) return false;
    if (snapshot.offline) {
      wasOffline = true;
      return false;
    }
    const next = contentRevision(snapshot.data);
    const changed = wasOffline || (previous !== undefined && previous !== next);
    previous = next;
    wasOffline = false;
    if (!changed) return false;
    await client.invalidateQueries({
      predicate: ({ queryKey }) =>
        queryKey[0] === prefix[0] &&
        queryKey[1] === prefix[1] &&
        CONTENT_QUERIES.has(String(queryKey[2])),
    });
    return true;
  };
}

/** Apply an acknowledged bookmark before any following detail refetch. */
export async function syncArticleBookmark(
  client: QueryClient,
  prefix: string[],
  article: Article,
  saved: boolean,
) {
  const queryKey = [...prefix, "article", article.id];
  await client.cancelQueries({ queryKey, exact: true });
  const snapshot = client.setQueryData<{ data: Article; offline: boolean }>(
    queryKey,
    (previous) => ({
      data: {
        ...(previous?.data.id === article.id ? previous.data : article),
        saved,
      },
      offline: previous?.offline ?? false,
    }),
  );
  return snapshot!.data;
}

/** TanStack's native focus bridge; the web keeps its visibilitychange listener. */
export function subscribeNativeFocus(
  appState: {
    currentState: string | null;
    addEventListener: (
      event: "change",
      listener: (state: string) => void,
    ) => { remove: () => void };
  },
  platform: string,
  focus: { setFocused: (focused: boolean | undefined) => void },
) {
  if (platform === "web") return () => {};
  const update = (state: string) => focus.setFocused(state === "active");
  if (appState.currentState !== null) update(appState.currentState);
  const subscription = appState.addEventListener("change", update);
  return () => {
    subscription.remove();
    focus.setFocused(undefined);
  };
}
