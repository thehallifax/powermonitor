#!/usr/bin/env python3
"""Benchmark local report generation for representative periods."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
COLLECTOR_ROOT = PROJECT_ROOT / "collector"
if str(COLLECTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_ROOT))

import report
from periods import resolve_period


PERIOD_ALIASES = {
    "current": "current_billing_period",
    "current_billing_period": "current_billing_period",
    "last_7_days": "last_7_days",
    "last_30_days": "last_30_days",
    "year_to_date": "year_to_date",
    "last_12_months": "last_12_months",
    "custom": "custom",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark PowerMonitor report rendering.")
    parser.add_argument("--period", choices=sorted(PERIOD_ALIASES), default="current_billing_period")
    parser.add_argument("--start", help="Custom start date, YYYY-MM-DD")
    parser.add_argument("--end", help="Custom end date, YYYY-MM-DD")
    parser.add_argument("--repeat", type=int, default=1, help="Number of identical renders to run")
    args = parser.parse_args()

    report.configure_demo_mode()
    settings = report.load_settings()
    today = report.local_today()
    period, start, end = resolve_benchmark_period(args.period, settings, today, args.start, args.end)
    results = []
    for run_number in range(max(1, args.repeat)):
        query_counter = QueryCounter()
        started = time.perf_counter()
        with query_counter.patch():
            document = report.build_live_html(period.kind, start=start, end=end)
        elapsed_ms = (time.perf_counter() - started) * 1000
        diagnostics = report.performance_diagnostics()
        diagnostics["query_count"] = query_counter.count
        diagnostics["html_bytes"] = len(document.encode("utf-8"))
        diagnostics["wall_clock_ms"] = round(elapsed_ms, 3)
        diagnostics["run"] = run_number + 1
        results.append(diagnostics)

    payload = {
        "period": {
            "kind": period.kind,
            "label": period.label,
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
        },
        "runs": results,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def resolve_benchmark_period(kind: str, settings, today, start: Optional[str], end: Optional[str]):
    normalized = PERIOD_ALIASES[kind]
    if normalized == "last_12_months":
        selected = resolve_period(
            "custom",
            settings,
            today,
            (today - timedelta(days=364)).isoformat(),
            today.isoformat(),
        )
        return selected, selected.start.isoformat(), selected.end.isoformat()
    if normalized == "custom":
        if not start or not end:
            raise SystemExit("--start and --end are required for --period custom")
        selected = resolve_period("custom", settings, today, start, end)
        return selected, selected.start.isoformat(), selected.end.isoformat()
    selected = resolve_period(normalized, settings, today)
    return selected, None, None


class QueryCounter:
    def __init__(self) -> None:
        self.count = 0
        self._original_connect = sqlite3.connect

    def patch(self):
        return self

    def __enter__(self):
        original = self._original_connect

        def counted_connect(*args, **kwargs):
            conn = original(*args, **kwargs)
            conn.set_trace_callback(lambda _statement: self._increment())
            return conn

        sqlite3.connect = counted_connect
        return self

    def __exit__(self, exc_type, exc, traceback):
        sqlite3.connect = self._original_connect
        return False

    def _increment(self) -> None:
        self.count += 1


if __name__ == "__main__":
    main()
