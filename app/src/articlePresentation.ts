import type { Article } from "./types";

export interface QuotedPost {
  identity: string;
  publishedAt?: string;
  text: string;
  unavailable: boolean;
}

export function translatedArticle(a: Article) {
  return a.translation?.status === "ready" && !!a.text_zh;
}

export function displayHeadline(a: Article): { text: string; ai: boolean } {
  if (a.presentation?.status === "ready" && a.presentation.title_zh?.trim()) {
    return { text: a.presentation.title_zh.trim(), ai: true };
  }
  // Only the server can decide whether a saved analysis matches this article.
  // Neither a translated title nor a linked page is evidence of a post summary.
  return {
    text:
      a.translation?.status === "ready" && a.title_zh ? a.title_zh : a.title,
    ai: false,
  };
}

// Only the collector's complete, standalone context markers are presentation
// boundaries. Ordinary @mentions, square brackets and quoted sentences remain text.
const QUOTE = /^\[引用帖：(@[A-Za-z0-9_]{1,15}|作者未返回)，([^\]\n]+)\]\s*$/;
const UNAVAILABLE = "[引用帖不可用，未取得原文]";

function splitText(text: string): { body: string; quotes: QuotedPost[] } {
  const body: string[] = [];
  const quotes: QuotedPost[] = [];
  let current: { quote: QuotedPost; lines: string[] } | undefined;
  const finish = () => {
    if (current) current.quote.text = current.lines.join("\n").trim();
  };
  for (const line of text.split("\n")) {
    const match = QUOTE.exec(line.trim());
    const time = match?.[2];
    const valid =
      match &&
      (time === "发布时间未返回" ||
        (/^\d{4}-\d{2}-\d{2}(?:T.*)?$/.test(time!) &&
          Number.isFinite(Date.parse(time!))));
    if (valid || line.trim() === UNAVAILABLE) {
      finish();
      const quote: QuotedPost = {
        identity: valid ? match![1]! : "引用的发言",
        publishedAt: valid && time !== "发布时间未返回" ? time : undefined,
        text: "",
        unavailable: !valid,
      };
      quotes.push(quote);
      current = { quote, lines: [] };
    } else if (current) {
      current.lines.push(line);
    } else {
      body.push(line);
    }
  }
  finish();
  return { body: body.join("\n").trim(), quotes };
}

export function articleContent(a: Article, original = false) {
  const text = !original && translatedArticle(a) ? a.text_zh! : a.text;
  if (a.platform !== "x") return { body: text, quotes: [] as QuotedPost[] };
  const source = splitText(a.text);
  const displayed = splitText(text);
  // Never invent attribution when an older translation lost a context marker.
  const matching =
    source.quotes.length === displayed.quotes.length &&
    source.quotes.every(
      (q, i) =>
        q.identity === displayed.quotes[i]?.identity &&
        q.publishedAt === displayed.quotes[i]?.publishedAt &&
        q.unavailable === displayed.quotes[i]?.unavailable,
    );
  return matching ? displayed : { body: text, quotes: [] as QuotedPost[] };
}

export function authorInitials(author: string) {
  const words = author.trim().split(/\s+/);
  if (/^[A-Za-z]/.test(author)) {
    return (
      words.length > 1
        ? (words[0]?.[0] || "") + (words[words.length - 1]?.[0] || "")
        : author.slice(0, 2)
    ).toUpperCase();
  }
  return Array.from(author.trim()).slice(0, 1).join("") || "·";
}

export function publicationLabel(
  value: string,
  dateOnly = false,
  full = false,
) {
  const d = new Date(value);
  if (!Number.isFinite(d.getTime())) return "时间未提供";
  const day = `${d.getMonth() + 1}月${d.getDate()}日`;
  const clock = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `${full ? `${d.getFullYear()}年` : ""}${day}${dateOnly ? "" : ` ${clock}`}`;
}
