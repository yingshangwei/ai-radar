import type { MarketSample, MarketSeries } from "./marketTypes";

/** Find an actual plotted sample without carrying a quote across a missing segment. */
export function marketReading(
  series: MarketSeries,
  at: number,
): MarketSample | null {
  const tolerance = Math.max(0, series.lookupTolerance ?? 0);
  let best: MarketSample | null = null;
  for (const stroke of series.strokes) {
    if (
      !stroke.length ||
      at < stroke[0].time - tolerance ||
      at > stroke[stroke.length - 1].time + tolerance
    )
      continue;
    for (const point of stroke) {
      if (
        Number.isFinite(point.value) &&
        (!best || Math.abs(point.time - at) < Math.abs(best.time - at))
      )
        best = point;
    }
  }
  return best;
}
