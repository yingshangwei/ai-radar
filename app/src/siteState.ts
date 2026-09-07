export type SiteAction =
  "none" | "automatic" | "retry" | "login" | "restricted";
export type Site = {
  domain: string;
  total: number;
  pending: number;
  document_id: string;
  url: string;
  enabled: boolean;
  status: string;
  message: string;
  verified_at: string;
  statuses: Record<string, number>;
  action?: SiteAction;
  access_status?: string;
  retry_at?: string | null;
};

export function siteAction(site: Site): SiteAction {
  // Collection results take precedence over legacy browser-profile status.
  if (site.pending === 0) return "none";
  if (
    site.action &&
    ["none", "automatic", "retry", "login", "restricted"].includes(site.action)
  )
    return site.action;
  const counts = site.statuses || {};
  if (site.status === "auth_required" || counts.auth_required) return "login";
  if (
    ["access_restricted", "restricted", "blocked"].includes(site.status) ||
    counts.access_restricted ||
    counts.restricted
  )
    return "restricted";
  if (
    ["unavailable", "rate_limited", "error"].includes(site.status) ||
    counts.unavailable ||
    counts.rate_limited
  )
    return "retry";
  return "automatic";
}

export function sitePresentation(site: Site): {
  action: SiteAction;
  label: string;
  message: string;
} {
  const action = siteAction(site);
  const labels: Record<SiteAction, string> = {
    none: site.pending === 0 ? "正文已采集" : "已暂停",
    automatic: "等待自动采集",
    retry: "将自动重试",
    login: "需要登录",
    restricted: "网站限制访问",
  };
  const messages: Record<SiteAction, string> = {
    none:
      site.pending === 0
        ? "已取得正文，无需登录或手动补采。中文翻译和解读会继续处理。"
        : "此网站的自动读取已暂停。",
    automatic: "公开网页正在排队，系统会自动读取，无需逐个操作。",
    retry: "系统会稍后重试。也可以授权手机自动读取此网站。",
    login: "目标文章要求登录。在手机完成一次授权，随后自动补采。",
    restricted:
      "网站限制服务器访问。可在手机完成一次授权，之后自动补采这个网站的文章。",
  };
  const detailLabels: Record<string, string> = {
    blocked: "不支持的链接",
    restricted: "限制自动读取",
    too_large: "页面内容过大",
    rate_limited: "等待访问冷却",
    busy: "等待浏览器空闲",
    paused: "浏览器补采已暂停",
  };
  return {
    action,
    label: site.pending
      ? detailLabels[site.status] || labels[action]
      : labels.none,
    // Current servers report collection status separately from browser access.
    // Legacy "unverified" messages must never override successful collection.
    message:
      site.action && site.pending > 0 && site.message
        ? site.message
        : messages[action],
  };
}
