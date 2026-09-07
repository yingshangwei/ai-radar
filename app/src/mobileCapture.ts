import { READABILITY_SOURCE } from "./vendor/readability";

export type MobileArticle = {
  url: string;
  title: string;
  text: string;
  links: { url: string; label: string }[];
  partial: boolean;
};
export const MAX_CAPTURE_CHARS = 60000;
const MAX_MESSAGE_CHARS = 1500000;
export class CaptureFailure extends Error {
  constructor(
    public reason: "verification" | "unavailable",
    message: string,
  ) {
    super(message);
  }
}

export function isWebURL(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      ["http:", "https:"].includes(url.protocol) &&
      !!url.hostname &&
      !url.username &&
      !url.password &&
      value.length <= 4000
    );
  } catch {
    return false;
  }
}

export function samePageURL(a: string, b: string): boolean {
  if (!isWebURL(a) || !isWebURL(b)) return false;
  const first = new URL(a);
  const second = new URL(b);
  // React Native's built-in URL has getters but no hash/protocol setters.
  return first.href.split("#", 1)[0] === second.href.split("#", 1)[0];
}

// This nonce correlates a single user-triggered extraction; it is not a credential.
// The web page is untrusted. Native code separately checks the saved site permission
// and exact server-selected target before it can submit a reply automatically.
export function captureNonce(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

export function parseCaptureMessage(
  raw: string,
  pending: { nonce: string; url: string } | undefined,
  eventURL: string,
  nativePageURL: string = eventURL,
): MobileArticle | null {
  if (!pending || raw.length > MAX_MESSAGE_CHARS) return null;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!value || typeof value !== "object") return null;
  const message = value as Record<string, unknown>;
  if (
    message.type !== "radar.article" ||
    message.nonce !== pending.nonce ||
    !samePageURL(nativePageURL, pending.url) ||
    // Android WebMessageListener reports sourceOrigin, while iOS reports the
    // full page URL. An origin-only event requires the separately observed
    // native navigation URL above; another path on the same host is not enough.
    (!samePageURL(eventURL, pending.url) &&
      eventURL !== new URL(pending.url).origin)
  )
    return null;
  if (typeof message.error === "string")
    throw new CaptureFailure(
      message.error === "verification_required"
        ? "verification"
        : "unavailable",
      message.error === "verification_required"
        ? "手机访问需要重新验证。完成网页上的登录或人机验证后会自动继续。"
        : "网页正文尚未就绪，稍后会自动重试。",
    );
  if (
    typeof message.url !== "string" ||
    !samePageURL(message.url, pending.url) ||
    typeof message.title !== "string" ||
    message.title.length > 500 ||
    typeof message.text !== "string" ||
    message.text.trim().length < 80 ||
    message.text.length > MAX_CAPTURE_CHARS ||
    typeof message.partial !== "boolean" ||
    !Array.isArray(message.links) ||
    message.links.length > 30
  )
    throw new Error(
      "正文格式不完整，请等待文章加载完成后重试，或改用粘贴正文。",
    );
  const links: MobileArticle["links"] = [];
  for (const link of message.links) {
    if (
      !link ||
      typeof link !== "object" ||
      typeof link.url !== "string" ||
      !isWebURL(link.url) ||
      typeof link.label !== "string" ||
      link.label.length > 300
    )
      throw new Error("正文链接格式不正确，请重新读取。");
    links.push({ url: link.url, label: link.label });
  }
  return {
    url: message.url,
    title: message.title.trim(),
    text: message.text.trim(),
    links,
    partial: message.partial,
  };
}

export function extractionScript(nonce: string): string {
  return `(function () {
    if (window.top !== window.self) return;
    var requestNonce = ${JSON.stringify(nonce)};
    function reply(value) {
      window.ReactNativeWebView.postMessage(JSON.stringify(Object.assign({type: "radar.article", nonce: requestNonce}, value)));
    }
    try {
      // Scope the upstream CommonJS footer away from any page-owned module object.
      var module = undefined;
      ${READABILITY_SOURCE}
      if (!/^https?:$/.test(location.protocol)) throw new Error("unsupported_url");
      var original = document.querySelectorAll("*");
      if (original.length > 50000) throw new Error("page_too_large");
      // Inspect only element type, layout and frame origin, never input values or
      // frame contents. A visible login/challenge overlay must not be stripped
      // away and mistaken for permission to read the article underneath it.
      function visibleControl(node) {
        if (!node.getClientRects().length) return false;
        for (var parent = node; parent && parent.nodeType === 1; parent = parent.parentElement) {
          var computed = window.getComputedStyle(parent);
          if (parent.hidden || parent.getAttribute("aria-hidden") === "true" || computed.display === "none" || computed.visibility === "hidden" || computed.visibility === "collapse" || computed.opacity === "0") return false;
        }
        return true;
      }
      var credentialsVisible = Array.from(document.querySelectorAll('input[type="password"]')).some(visibleControl);
      var challengeVisible = Array.from(document.querySelectorAll('iframe[src]')).some(function (frame) {
        if (!visibleControl(frame)) return false;
        try {
          var frameURL = new URL(frame.getAttribute("src"), location.href);
          return frameURL.hostname === "challenges.cloudflare.com" || frameURL.hostname === "hcaptcha.com" || frameURL.hostname.endsWith(".hcaptcha.com") || ((frameURL.hostname === "www.google.com" || frameURL.hostname === "www.recaptcha.net") && frameURL.pathname.indexOf("/recaptcha/") === 0);
        } catch (_) { return false; }
      });
      if (credentialsVisible || challengeVisible) { reply({error: "verification_required"}); return; }
      var copy = document.cloneNode(true);
      var cloned = copy.querySelectorAll("*");
      // Remove hidden content using computed styles on the live DOM, before parsing
      // its detached clone. Never inspect cookies, storage, input values or frames.
      for (var i = 0; i < original.length; i++) {
        if (!cloned[i] || !cloned[i].parentNode) continue;
        var style = window.getComputedStyle(original[i]);
        if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") cloned[i].remove();
      }
      copy.querySelectorAll('form,input,textarea,select,button,[contenteditable]:not([contenteditable="false"]),[role="textbox"],script,style,noscript,iframe,frame,object,embed,nav,header,footer,aside,[hidden],[aria-hidden="true"],[role="dialog"],[role="alertdialog"]').forEach(function (node) { node.remove(); });
      var pageTitle = (document.title || "").trim();
      var safeText = (copy.body && copy.body.textContent || "").trim();
      if (/^(just a moment|access denied|verify (you|that you)|sign in|log in|登录|人机验证)(\\b|\\s|[·|—-])/i.test(pageTitle) || (safeText.length < 1500 && /verifying you are human|verify you are human|performing security verification|enable javascript and cookies to continue|正在验证您是否是真人/i.test(safeText))) {
        reply({error: "verification_required"}); return;
      }
      var article = new Readability(copy, { disableJSONLD: true, maxElemsToParse: 50000, charThreshold: 80, serializer: function (node) { return node; } }).parse();
      if (!article || !article.content) throw new Error("no_article");
      var content = article.content;
      var links = [];
      var seen = new Set();
      content.querySelectorAll("a[href]").forEach(function (node) {
        if (links.length >= 30) return;
        try {
          var target = new URL(node.getAttribute("href"), location.href);
          if (!/^https?:$/.test(target.protocol) || target.username || target.password || target.href.length > 4000) return;
          target.hash = "";
          if (seen.has(target.href)) return;
          seen.add(target.href);
          links.push({ url: target.href, label: (node.textContent || "").trim().slice(0, 300) });
        } catch (_) {}
      });
      // Retain paragraph boundaries in plain text; do not send or render article HTML.
      content.querySelectorAll("p,h1,h2,h3,h4,h5,h6,li,blockquote,pre,tr,br").forEach(function (node) {
        node.appendChild(copy.createTextNode("\\n\\n"));
      });
      var text = (content.textContent || "").replace(/[\\t ]+/g, " ").replace(/ *\\n */g, "\\n").replace(/\\n{3,}/g, "\\n\\n").trim();
      if (text.length < 80) throw new Error("no_article");
      reply({ url: location.href, title: (article.title || document.title || "").slice(0,500), text: text.slice(0, ${MAX_CAPTURE_CHARS}), links: links, partial: text.length > ${MAX_CAPTURE_CHARS} });
    } catch (_) { reply({error: "no_article"}); }
  })(); true;`;
}
