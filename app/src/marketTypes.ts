export type MarketSample = {
  time: number;
  value: number;
  raw: number;
  run: number;
};
export type MarketSeries = {
  name: string;
  strokes: MarketSample[][];
  minimum: number;
  maximum: number;
};
export type MarketChart = {
  source: string;
  range: { start: number; end: number };
  series: MarketSeries[];
  observations: number;
};
export type MarketTier = {
  thresholdCNY: number;
  average?: number;
  filledCNY: number;
  merchantCount: number;
  largestMerchantShare?: number;
};
export type MarketWaterline = {
  observedAt: number;
  complete: boolean;
  eligibleAds: number;
  unknownQualityAds: number;
  tiers: MarketTier[];
};
export type MarketView = {
  schema: number;
  generatedAt: number;
  collectorHost: string;
  dashboard: {
    config: { intervalSeconds: number; pairs: string[] };
    service: { running: boolean };
    latestCycle?: {
      endedAt: number;
      succeeded: number;
      failed: number;
      skipped: number;
    };
    streams: {
      id: string;
      lastSuccess?: number;
      blocked: boolean;
      error?: string;
      nextAt: number;
    }[];
    tickers: {
      pair: string;
      last: number;
      open24h: number;
      observedAt: number;
    }[];
    counts: Record<string, number>;
  };
  waterlines: Record<string, MarketWaterline>;
  pressure: { status: string; score?: number; baselineCoverage: number };
  charts: Record<string, MarketChart>;
  storage: { rawDays: number; metricDays: number };
};
