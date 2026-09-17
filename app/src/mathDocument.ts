import MarkdownIt from "markdown-it";
import { katexCss, katexJs, katexLicense } from "./generated/katexAssets";

const markdown = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
  typographer: false,
});
markdown.validateLink = (url: string) =>
  /^https?:\/\//i.test(url) && !/[\u0000-\u0020]/.test(url);
markdown.renderer.rules.image = (tokens, index) =>
  markdown.utils.escapeHtml(tokens[index]?.content || "图片");

// Presentation only: no source or translated text is changed in storage. Leave
// structured Markdown, math and code intact; split unusually long prose at sentences.
export function readableParagraphs(text: string): string {
  return text.replace(/```[\s\S]*?```|~~~[\s\S]*?~~~|[^\n]+/g, (block) => {
    if (
      block.length < 450 ||
      /^(?:\s|[#>|*`~]|\d+\.)/.test(block) ||
      /RADARMATH|\$|\\[([]/.test(block)
    )
      return block;
    let length = 0;
    return block.replace(
      /[^。！？.!?]+[。！？.!?]+(?:[”’"']|(?=\s))?\s*/g,
      (sentence) => {
        length += sentence.length;
        if (length < 280) return sentence;
        length = 0;
        return sentence + "\n\n";
      },
    );
  });
}
export function richPlainText(text: string): string {
  const tokens = markdown.parse(text, {});
  return tokens
    .filter((t) => ["inline", "fence", "code_block"].includes(t.type))
    .map((t) =>
      t.type !== "inline"
        ? t.content
        : (t.children || [])
            .map((c) =>
              ["text", "code_inline"].includes(c.type)
                ? c.content
                : c.type.endsWith("break")
                  ? " "
                  : "",
            )
            .join(""),
    )
    .join("\n\n");
}
export function richPreview(text: string): string {
  return richPlainText(mathPreview(text)).replace(/\s+/g, " ");
}

export interface MathSpan {
  start: number;
  end: number;
  source: string;
  tex: string;
  display: boolean;
}
export function mathSpans(text: string): MathSpan[] {
  const token =
    /```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]|\$\$[\s\S]*?\$\$|(?<![\\$])\$(?!\$)([^\n$]+?)(?<!\\)\$(?!\$)|\\begin\{(?<env>equation\*?|align\*?|aligned|gather\*?|multline\*?)\}[\s\S]*?\\end\{\k<env>\}/g;
  const spans: MathSpan[] = [];
  for (const match of text.matchAll(token)) {
    const source = match[0];
    if (source.startsWith("`") || source.startsWith("~~~")) continue;
    const inlineDollar = source.startsWith("$") && !source.startsWith("$$");
    if (inlineDollar && source.slice(1, -1) !== source.slice(1, -1).trim())
      continue;
    const environment = source.startsWith("\\begin");
    spans.push({
      start: match.index!,
      end: match.index! + source.length,
      source,
      tex: environment
        ? source
        : source.slice(inlineDollar ? 1 : 2, inlineDollar ? -1 : -2),
      display:
        source.startsWith("$$") || source.startsWith("\\[") || environment,
    });
  }
  return spans;
}

const escapeHtml = (text: string) =>
  text.replace(
    /[&<>"']/g,
    (value) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        value
      ]!,
  );

// Preview cards stay native and light; full detail always keeps the complete formula.
export function mathPreview(text: string): string {
  for (const span of mathSpans(text).reverse())
    text =
      text.slice(0, span.start) + "〔公式，详情查看〕" + text.slice(span.end);
  return text;
}

export function mathDocument(
  text: string,
  options: {
    nonce: string;
    fontSize: number;
    lineHeight: number;
    color: string;
    fontWeight?: string;
  },
): string {
  if (!/^[a-z0-9]+$/i.test(options.nonce))
    throw new Error("Invalid math view nonce");
  const formulas: string[] = [];
  let cursor = 0,
    source = "";
  for (const span of mathSpans(text)) {
    source += text.slice(cursor, span.start);
    if (formulas.length < 256 && span.tex.length <= 12000) {
      const token = `RADARMATH${options.nonce}TOKEN${formulas.length}END`;
      formulas.push(
        '<span class="formula ' +
          (span.display ? "display" : "inline") +
          '" data-tex="' +
          escapeHtml(span.tex) +
          '" data-source="' +
          escapeHtml(span.source) +
          '" data-display="' +
          span.display +
          '">' +
          escapeHtml(span.source) +
          "</span>",
      );
      source += span.display ? "\n\n" + token + "\n\n" : token;
    } else source += span.source;
    cursor = span.end;
  }
  source += text.slice(cursor);
  let body = markdown.render(readableParagraphs(source));
  formulas.forEach((formula, index) => {
    body = body.replace(
      `RADARMATH${options.nonce}TOKEN${index}END`,
      () => formula,
    );
  });
  const font = Math.min(64, Math.max(10, options.fontSize));
  const line = Math.max(font * 1.4, Math.min(100, options.lineHeight));
  const color = /^#[0-9a-f]{3,8}$/i.test(options.color)
    ? options.color
    : "#293326";
  const weight = /^(?:normal|bold|[1-9]00)$/.test(options.fontWeight || "")
    ? options.fontWeight
    : "normal";
  const runtime =
    "(function(){" +
    "var nonce=" +
    JSON.stringify(options.nonce) +
    ";" +
    'document.querySelectorAll(".formula").forEach(function(el){try{' +
    'katex.render(el.dataset.tex,el,{displayMode:el.dataset.display==="true",throwOnError:true,trust:false,strict:"warn",maxSize:20,maxExpand:300,macros:{},output:"htmlAndMathml"});' +
    '}catch(e){el.textContent=el.dataset.source;el.classList.add("fallback");}});' +
    'function report(){var message={type:"math-size",nonce:nonce,height:Math.ceil(document.getElementById("content").getBoundingClientRect().height)+4};' +
    'if(window.ReactNativeWebView)window.ReactNativeWebView.postMessage(JSON.stringify(message));else window.parent.postMessage(message,"*");}' +
    'document.addEventListener("click",function(e){var a=e.target.closest("a");if(!a)return;e.preventDefault();var m={type:"rich-link",nonce:nonce,url:a.getAttribute("href")};if(window.ReactNativeWebView)window.ReactNativeWebView.postMessage(JSON.stringify(m));else window.parent.postMessage(m,"*");});' +
    'new ResizeObserver(report).observe(document.getElementById("content"));window.addEventListener("resize",report);' +
    "document.fonts.ready.then(report);requestAnimationFrame(report);})();";
  return (
    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
    "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'nonce-" +
    options.nonce +
    "'; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'\">" +
    "<style>" +
    (formulas.length ? katexCss : "") +
    '\nhtml,body{margin:0;padding:0;background:transparent}body{font-family:-apple-system,BlinkMacSystemFont,\"Noto Sans\",sans-serif;font-size:' +
    font +
    "px;line-height:" +
    line +
    "px;font-weight:" +
    weight +
    ";color:" +
    color +
    ";-webkit-text-size-adjust:100%}" +
    "#content{overflow-wrap:anywhere;padding:2px 0}p{margin:0 0 .85em}p:last-child{margin-bottom:0}h1,h2,h3,h4{line-height:1.45;margin:1.1em 0 .5em;font-size:1.13em}h1{font-size:1.35em}h2{font-size:1.2em}strong{font-weight:700;color:#18281f}ul,ol{padding-left:1.45em;margin:.5em 0 1em}li{margin:.3em 0}blockquote{border-left:3px solid #a4b6a6;background:#edf1e9;padding:.6em .9em;margin:.8em 0;color:#52614f}pre{overflow-x:auto;white-space:pre;background:#edf0e8;border-radius:8px;padding:12px;font-size:.85em;line-height:1.6}code{font-family:ui-monospace,monospace;background:#edf0e8;font-size:.9em;padding:.1em .25em}pre code{padding:0}table{border-collapse:collapse;display:block;overflow-x:auto;max-width:100%;font-size:.9em;margin:1em 0}td,th{border:1px solid #d9dfd3;padding:7px 10px;text-align:left;min-width:90px}th{background:#edf0e8}a{color:#426d52;text-decoration:underline}hr{border:0;border-top:1px solid #d9dfd3;margin:1em 0}.formula{white-space:normal;max-width:100%;overflow-x:auto;overflow-y:hidden;box-sizing:border-box}.inline{display:inline-block;vertical-align:middle}.display{display:block;padding:9px 0;margin:8px 0;overscroll-behavior-x:contain}.katex{font-size:1.05em}.katex-display{margin:0;padding:4px 2px;text-align:left}.katex-display>.katex{text-align:left}.fallback{white-space:pre-wrap;font-family:monospace;font-size:.9em}*{box-sizing:border-box}</style>" +
    '</head><body><div id="content">' +
    body +
    "</div><!--" +
    katexLicense.replace(/-->/g, "--&gt;") +
    "-->" +
    '<script nonce="' +
    options.nonce +
    '">' +
    (formulas.length ? katexJs.replace(/<\/script/gi, "<\\/script") : "") +
    "</script>" +
    '<script nonce="' +
    options.nonce +
    '">' +
    runtime +
    "</script></body></html>"
  );
}
