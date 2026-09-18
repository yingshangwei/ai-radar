import type { Article } from "./types";

export type AuthorOption = {
  key: string;
  platform: Article["platform"];
  handle: string;
  name: string;
  count: number;
};

export function authorKey(
  article: Pick<Article, "platform" | "handle" | "author">,
) {
  const handle = (article.handle || "").trim().toLowerCase();
  return handle
    ? `${article.platform}:handle:${handle}`
    : `${article.platform}:name:${article.author}`;
}

export function authorParams(params: URLSearchParams) {
  const scope = new URLSearchParams(params.toString());
  for (const key of ["author", "limit", "offset", "sort"]) scope.delete(key);
  return scope.toString();
}

export function matchingAuthors(items: AuthorOption[], search: string) {
  const needle = search.trim().replace(/^@/, "").toLowerCase();
  return items.filter((a) =>
    `${a.name} ${a.handle}`.toLowerCase().includes(needle),
  );
}

export function demoAuthorOptions(articles: Article[]): AuthorOption[] {
  const values = new Map<string, AuthorOption>();
  for (const a of articles) {
    const key = authorKey(a);
    const row = values.get(key) || {
      key,
      platform: a.platform,
      handle: a.handle,
      name: a.author,
      count: 0,
    };
    row.count++;
    values.set(key, row);
  }
  return [...values.values()].sort(
    (a, b) => b.count - a.count || a.key.localeCompare(b.key),
  );
}
