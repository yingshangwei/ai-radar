export type TokenCounts = {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cached_tokens: number | null;
  cache_write_tokens: number | null;
  reasoning_tokens: number | null;
};
export type UsageGroup = TokenCounts & {
  provider: string;
  model: string;
  feature: string;
  stage: string;
  calls: number;
  reported_calls: number;
  unknown_calls: number;
  active_calls: number;
};
export type UsageReport = {
  enabled: boolean;
  available: boolean;
  error?: string;
  tracking_started_at: string;
  as_of: string;
  timezone: string;
  recording_errors: number;
  totals: UsageGroup;
  groups: UsageGroup[];
  recent: (TokenCounts & {
    id: string;
    started_at: string;
    provider: string;
    model: string;
    feature: string;
    stage: string;
    outcome: string;
    model_basis: string;
  })[];
  allocation: {
    feature: string;
    stage: string;
    provider: string;
    model: string;
  }[];
  features: Record<string, string>;
  stages: Record<string, string>;
};

export function tokenAmount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value < 0) return "—";
  const units = ["", "K", "M", "B"];
  let amount = value,
    unit = 0;
  while (amount >= 1000 && unit < 3) {
    amount /= 1000;
    unit++;
  }
  // Promote at the rounding boundary as well: never display 1000 K.
  if (unit < 3 && Number(amount.toFixed(2)) >= 1000) {
    amount /= 1000;
    unit++;
  }
  return `${Number(amount.toFixed(unit ? 2 : 0))}${units[unit]}`;
}

export const providerName = (value: string) =>
  ({
    bailian: "阿里百炼",
    codex: "Codex CLI",
    deepseek: "DeepSeek",
    openai: "OpenAI",
    openai_chat: "OpenAI",
    anthropic: "Anthropic",
    claude_cli: "Claude Code",
    openai_compatible: "兼容 API",
    command: "Agent CLI",
  })[value] || value;
export const modelName = (value: string) =>
  value === "auto-unreported" ? "自动选择 · 未报告型号" : value;

export function modelTotals(groups: UsageGroup[]) {
  const result = new Map<
    string,
    { model: string; provider: string; tokens: number | null; calls: number }
  >();
  for (const group of groups) {
    const key = `${group.provider}/${group.model}`;
    const row = result.get(key) || {
      model: group.model,
      provider: group.provider,
      tokens: null,
      calls: 0,
    };
    if (group.total_tokens != null)
      row.tokens = (row.tokens || 0) + group.total_tokens;
    row.calls += group.calls;
    result.set(key, row);
  }
  return [...result.values()].sort((a, b) => (b.tokens || 0) - (a.tokens || 0));
}
