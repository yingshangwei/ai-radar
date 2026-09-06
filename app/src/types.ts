export interface Article {
  id: string;
  platform: "x" | "facebook" | "rss" | "web";
  source_id: string;
  title: string;
  text: string;
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
}
export interface Source {
  id: string;
  name: string;
  platform: string;
  status: string;
  message: string;
  last_success_at?: string;
}
export interface Status {
  timezone: string;
  daily_time: string;
  provider: string;
  model?: string;
  scheduler_enabled: boolean;
  article_count: number;
  sources: Source[];
  jobs: {
    id: string;
    kind: string;
    status: string;
    message: string;
    started_at: string;
  }[];
}
export interface Connection {
  url: string;
  token: string;
  demo?: boolean;
}
