export type IndustryState =
  "improving" | "mixed" | "weakening" | "insufficient_evidence";

export type IndustryClaim = { text_zh: string; source_ids: string[] };

export type IndustryAssessment = {
  id: string;
  status: "ready" | "pending" | "needs_attention" | "outcome_unknown";
  as_of: string;
  created_at: string;
  state: IndustryState;
  summary_zh: string;
  supporting: IndustryClaim[];
  opposing: IndustryClaim[];
  investment_implications: IndustryClaim[];
  watch_items: IndustryClaim[];
  unknowns: string[];
  horizon: string;
  evidence_ids: string[];
};

export type IndustryTheme = {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  hypothesis: string;
  indicators: string[];
  risks: string[];
  companies: { id: string; name: string; ticker: string; exchange: string }[];
  evidence_count: number;
  independent_sources: number;
  latest_evidence_at: string | null;
  state: IndustryState;
  assessment: IndustryAssessment | null;
  previous_assessment: IndustryAssessment | null;
  analysis_status?: {
    status: string;
    failure_code?: string | null;
    updated_at?: string | null;
  } | null;
};

export type IndustrySource = {
  id: string;
  name: string;
  description: string;
  status: "pending" | "healthy" | "error" | "disabled";
  message: string;
  last_attempt_at: string | null;
  last_success_at: string | null;
  item_count: number;
  refresh_minutes: number;
  url: string;
  limitations?: string;
};

export type IndustryJob = {
  id: string;
  status: string;
  message: string;
  queued_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
};

export type IndustryReport = {
  enabled: boolean;
  free_only: true;
  as_of: string;
  scope: string;
  limitations: string[];
  sources: IndustrySource[];
  themes: IndustryTheme[];
  latest_job?: IndustryJob | null;
  budget?: {
    calls_today: number;
    max_calls_per_day: number;
    analysis_enabled: boolean;
    unit: "model_stage";
    provider_calls_per_stage_max: number;
  };
};

export type IndustryEvidence = {
  id: string;
  source_id: string;
  source_name: string;
  url: string;
  title: string;
  text: string;
  published_at: string | null;
  published_precision: string;
  first_seen_at: string;
  kind: string;
  entity_ids: string[];
  theme_ids: string[];
  partial: boolean;
  revision: number;
};

export type IndustryEvidencePage = { items: IndustryEvidence[]; total: number };
