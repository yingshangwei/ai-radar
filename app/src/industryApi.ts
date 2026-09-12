import { api, APIError } from "./api";
import type { Connection } from "./types";
import type {
  IndustryEvidence,
  IndustryEvidencePage,
  IndustryJob,
  IndustryReport,
} from "./industryTypes";

export const fetchIndustry = (connection: Connection, signal?: AbortSignal) =>
  api<IndustryReport>(connection, "/v1/industry", { signal });

export function fetchIndustryEvidence(
  connection: Connection,
  options: { theme: string; offset?: number; q?: string; signal?: AbortSignal },
) {
  const params = new URLSearchParams({
    theme: options.theme,
    limit: "30",
    offset: String(options.offset || 0),
  });
  if (options.q?.trim()) params.set("q", options.q.trim());
  return api<IndustryEvidencePage>(
    connection,
    `/v1/industry/evidence?${params.toString()}`,
    { signal: options.signal },
  );
}

export const fetchIndustryEvidenceById = (
  connection: Connection,
  id: string,
  signal?: AbortSignal,
) =>
  api<IndustryEvidence>(
    connection,
    `/v1/industry/evidence/${encodeURIComponent(id)}`,
    { signal },
  );

export const refreshIndustry = (connection: Connection) =>
  api<IndustryJob>(connection, "/v1/industry/refresh", { method: "POST" });

export const fetchIndustryJob = (
  connection: Connection,
  id: string,
  signal?: AbortSignal,
) =>
  api<IndustryJob>(connection, `/v1/industry/jobs/${encodeURIComponent(id)}`, {
    signal,
  });

export const setIndustryTracking = (
  connection: Connection,
  id: string,
  enabled: boolean,
) =>
  api<{ id: string; enabled: boolean }>(
    connection,
    `/v1/industry/themes/${encodeURIComponent(id)}/tracking`,
    {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    },
  );

export const industryAccessDenied = (error: unknown) =>
  error instanceof APIError && [401, 403].includes(error.status);

export function industryError(
  error: unknown,
  resource: "report" | "evidence" | "job" = "report",
) {
  if (error instanceof APIError && error.status === 404) {
    if (resource === "evidence") return "这条原始证据暂不可用，无法核对引用。";
    if (resource === "job")
      return "无法取得这项任务的最新状态，请刷新行业页面核对。";
    return "当前服务器尚未提供行业研究，请更新服务器后重试。";
  }
  if (industryAccessDenied(error))
    return "行业数据访问未通过验证，请检查连接令牌。";
  return error instanceof Error ? error.message : "请求未完成，请稍后重试。";
}
