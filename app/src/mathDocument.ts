import { katexCss, katexJs, katexLicense } from "./generated/katexAssets";

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
  let cursor = 0,
    body = "",
    count = 0;
  for (const span of mathSpans(text)) {
    body += escapeHtml(text.slice(cursor, span.start));
    if (++count <= 256 && span.tex.length <= 12000) {
      const tag = span.display ? "div" : "span";
      body +=
        "<" +
        tag +
        ' class="formula ' +
        (span.display ? "display" : "inline") +
        '" data-tex="' +
        escapeHtml(span.tex) +
        '" data-source="' +
        escapeHtml(span.source) +
        '" data-display="' +
        span.display +
        '">' +
        escapeHtml(span.source) +
        "</" +
        tag +
        ">";
    } else body += escapeHtml(span.source);
    cursor = span.end;
  }
  body += escapeHtml(text.slice(cursor));
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
    'new ResizeObserver(report).observe(document.getElementById("content"));window.addEventListener("resize",report);' +
    "document.fonts.ready.then(report);requestAnimationFrame(report);})();";
  return (
    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
    "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'nonce-" +
    options.nonce +
    "'; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'\">" +
    "<style>" +
    katexCss +
    '\nhtml,body{margin:0;padding:0;background:transparent}body{font-family:-apple-system,BlinkMacSystemFont,\"Noto Sans\",sans-serif;font-size:' +
    font +
    "px;line-height:" +
    line +
    "px;font-weight:" +
    weight +
    ";color:" +
    color +
    ";-webkit-text-size-adjust:100%}" +
    "#content{white-space:pre-wrap;overflow-wrap:anywhere;padding:2px 0}.formula{white-space:normal;max-width:100%;overflow-x:auto;overflow-y:hidden;box-sizing:border-box}.inline{display:inline-block;vertical-align:middle}.display{display:block;padding:9px 0;margin:8px 0;overscroll-behavior-x:contain}.katex{font-size:1.05em}.katex-display{margin:0;padding:4px 2px;text-align:left}.katex-display>.katex{text-align:left}.fallback{white-space:pre-wrap;font-family:monospace;font-size:.9em}*{box-sizing:border-box}</style>" +
    '</head><body><div id="content">' +
    body +
    "</div><!--" +
    katexLicense.replace(/-->/g, "--&gt;") +
    "-->" +
    '<script nonce="' +
    options.nonce +
    '">' +
    katexJs.replace(/<\/script/gi, "<\\/script") +
    "</script>" +
    '<script nonce="' +
    options.nonce +
    '">' +
    runtime +
    "</script></body></html>"
  );
}
