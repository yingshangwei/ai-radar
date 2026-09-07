export const sourceConnected = (status: string) =>
  status === "healthy" || status === "partial";

export function sourceStatusLabel(status: string): string {
  return (
    (
      {
        healthy: "已连接",
        partial: "部分覆盖",
        auth_required: "待授权",
        rate_limited: "额度受限",
        error: "采集异常",
        pending: "等待采集",
        preview: "示例",
      } as Record<string, string>
    )[status] || status
  );
}
