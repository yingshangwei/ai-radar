export type AccountWindow = {
  name: string;
  used_percent: number;
  remaining_percent: number;
  window_minutes: number;
  resets_at: string;
  expired: boolean;
};
export type AccountStatus = {
  id: string;
  provider: string;
  name: string;
  in_use: boolean;
  status: string;
  message: string;
  stale: boolean;
  checking: boolean;
  last_success_at: string | null;
  checked_at: string | null;
  next_check_at: string | null;
  scope?: string;
  plan?: string | null;
  balances: {
    currency: string;
    amount: string;
    cash_amount: string | null;
    granted_amount: string | null;
  }[];
  limits: {
    id: string;
    name: string;
    windows: AccountWindow[];
    credits: {
      balance: string | null;
      unlimited: boolean | null;
      has_credits: boolean | null;
    } | null;
  }[];
};
export type AccountReport = {
  enabled: boolean;
  items: AccountStatus[];
  refreshing: boolean;
  as_of?: string;
  refresh_seconds?: number;
};

export function money(value: string | null | undefined, currency: string) {
  if (value == null || !value.trim() || !Number.isFinite(Number(value)))
    return "—";
  return `${currency === "CNY" ? "¥" : currency === "USD" ? "$" : currency + " "}${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
}

export function windowName(minutes: number) {
  if (minutes % 1440 === 0) return `${minutes / 1440} 天额度`;
  if (minutes % 60 === 0) return `${minutes / 60} 小时额度`;
  return `${minutes} 分钟额度`;
}

export function accountBadge(
  account: AccountStatus,
  offline = false,
  now = Date.now(),
) {
  if (offline) return "连接中断 · 历史数据";
  if (account.checking) return "正在更新";
  if (account.status === "authorization_required") return "待授权";
  if (account.status === "unsupported") return "暂不支持查询";
  if (account.status === "not_checked") return "等待查询";
  if (
    account.status === "query_failed" ||
    account.status === "invalid_response"
  )
    return "查询失败";
  if (
    account.stale ||
    (account.limits.length > 0 &&
      account.limits.every(
        (b) =>
          b.windows.length > 0 &&
          b.windows.every((w) => Date.parse(w.resets_at) <= now),
      ))
  )
    return "等待更新";
  return (
    (
      { ok: "已更新", low: "余额或额度偏低", exhausted: "余额不足" } as Record<
        string,
        string
      >
    )[account.status] || "状态未知"
  );
}
