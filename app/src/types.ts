export interface Article {
  id: string;
  platform: "x" | "facebook" | "rss" | "web";
  source_id: string;
  title: string;
  text: string;
  title_zh?: string | null;
  text_zh?: string | null;
  translation?: {
    status:
      | "disabled"
      | "pending"
      | "running"
      | "ready"
      | "review_required"
      | "insufficient_balance"
      | "error";
    updated_at?: string;
  };
  author: string;
  handle: string;
  url: string;
  published_at: string;
  published_precision?: "timestamp" | "date";
  metrics: Record<string, number>;
  topics: string[];
  score: number;
  priority: boolean;
  saved: boolean;
  resources?: ReadResource[];
  presentation?: {
    status: "ready" | "pending" | "review_required" | "error" | "disabled";
    title_zh: string | null;
  };
  social?: {
    reply_to: {
      author: string;
      handle: string;
      url: string;
      published_at: string | null;
    } | null;
  };
  discovery?: {
    kind: "early_signal";
    label: "潜力预判";
    reason_zh: string;
    uncertainty_zh: string;
    confidence: number;
    potential_impact: number;
    novelty: number;
    judged_at: string;
    initial_engagement: number;
    current_engagement: number;
    outcome: "pending" | "gaining_attention";
  };
}
export interface ReadResource {
  id: string;
  url: string;
  resolved_url: string;
  relation: "source" | "link" | "mention";
  title: string;
  title_zh?: string | null;
  status: string;
  fetch_status: string;
  capture_method?: string;
  message: string;
  partial: boolean;
  fetched_at?: string | null;
  summary_zh?: string | null;
  key_points_zh: string[];
  why_it_matters_zh?: string | null;
  translation_status: string;
  text?: string;
  text_zh?: string | null;
}
export interface Story {
  title: string;
  summary: string;
  why_it_matters: string;
  category: string;
  source_ids: string[];
}
export interface Digest {
  date: string;
  title: string;
  overview: string;
  stories: Story[];
  sources?: Article[];
  provider: string;
  model?: string;
  generated_at: string;
  window_start: string;
  window_end: string;
  source_count: number;
  coverage: Source[];
}
export interface Watch {
  id: string;
  name: string;
  handle: string;
  organization: string;
  role: string;
  enabled: boolean;
  discovery?: {
    origin: "automatic";
    status: "trial" | "retained" | "expired" | "user_stopped";
    expires_at: string;
    reason_zh: string;
  };
}
export interface Source {
  id: string;
  name: string;
  platform: string;
  status: string;
  message: string;
  last_success_at?: string;
}
export interface Job {
  id: string;
  kind: string;
  status: string;
  message: string;
  started_at?: string | null;
  finished_at?: string | null;
  queued_at?: string | null;
  phase?: string | null;
  heartbeat_at?: string | null;
  progress_at?: string | null;
  retry_at?: string | null;
  attempt?: number;
  max_attempts?: number;
  reason_code?: string | null;
}
export interface Status {
  timezone: string;
  daily_time: string;
  provider: string;
  model?: string;
  scheduler_enabled: boolean;
  article_count: number;
  server_now?: string;
  job_counts?: Record<string, number>;
  discovery?: {
    enabled: boolean;
    pending: number;
    unknown: number;
    error: number;
    calls_today: number;
    daily_call_limit: number;
    active_trials: number;
    entities_pending: number;
  };
  translation?: {
    enabled: boolean;
    configured: boolean;
    counts: Record<string, number>;
    resource_counts?: Record<string, number>;
    queue?: {
      counts: Record<string, number>;
      next_retry_at?: string | null;
    } | null;
    alert?: {
      code: "insufficient_balance";
      title: string;
      message: string;
      observed_at: string;
    } | null;
  };
  sources: Source[];
  jobs: Job[];
}
export interface Connection {
  url: string;
  token: string;
  demo?: boolean;
}
