"""Read durable cumulative totals into the official OpenTelemetry exporter.

Restarts and scrape failures cannot drop usage or trigger model calls. This
process has no model SDK, API keys, business DB, or outbound request capability.
"""

import argparse
import signal
import sqlite3
from pathlib import Path

from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from prometheus_client import start_http_server

FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens")


def read_totals(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        query = ",".join(f"SUM({field}) AS {field}" for field in FIELDS)
        return [dict(row) for row in db.execute(f"""SELECT provider,model,feature,stage,{query},
            COUNT(*) AS calls,
            SUM(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN 1 ELSE 0 END) AS reported
            FROM usage_calls GROUP BY provider,model,feature,stage""")]
    finally:
        db.close()


def setup(path):
    reader = PrometheusMetricReader(disable_target_info=True)
    provider = MeterProvider(resource=Resource({"service.name": "ai-radar-usage"}), metric_readers=[reader])
    meter = provider.get_meter("ai-radar.usage", "1.0")

    def tokens(_options):
        for row in read_totals(path):
            attributes = {key: row[key] for key in ("provider", "model", "feature", "stage")}
            for key in FIELDS:
                if row[key] is not None:
                    yield Observation(row[key], {**attributes, "token_type": key.removesuffix("_tokens")})

    def calls(_options):
        for row in read_totals(path):
            attributes = {key: row[key] for key in ("provider", "model", "feature", "stage")}
            yield Observation(row["calls"], attributes)

    def available(_options):
        try:
            read_totals(path)
            return [Observation(1)]
        except sqlite3.Error:
            return [Observation(0)]

    meter.create_observable_counter("radar_model_tokens", callbacks=[tokens], unit="tokens",
        description="Provider reported cumulative tokens; cache and reasoning are subsets, not additive")
    meter.create_observable_counter("radar_model_calls", callbacks=[calls],
        description="Physical API attempts and CLI turns, including unknown usage")
    meter.create_observable_gauge("radar_usage_ledger_available", callbacks=[available])
    return provider


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--port", type=int, default=18476)
    args = parser.parse_args()
    provider = setup(args.database)
    server, _ = start_http_server(args.port, addr="127.0.0.1")
    try:
        signal.pause()
    finally:
        server.shutdown()
        provider.shutdown()


if __name__ == "__main__":
    main()
