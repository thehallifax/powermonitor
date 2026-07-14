import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_ROOT = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import collector.report as report
from collector.performance import BoundedHtmlCache, memoize, performance_context, stage
from collector.benchmark_report import resolve_benchmark_period
from engine.data_engine import DataEngine


class ReportPerformanceTests(unittest.TestCase):
    def test_timing_and_request_memoisation_run_loader_once(self):
        calls = {"count": 0}

        with performance_context({"kind": "custom", "start": "2026-01-01", "end": "2026-01-31"}) as context:
            def loader():
                calls["count"] += 1
                return {"value": 42}

            with stage("expensive calculation"):
                first = memoize("expensive", {"range": "jan"}, loader)
                second = memoize("expensive", {"range": "jan"}, loader)

        diagnostics = context.as_dict()
        self.assertEqual(first, second)
        self.assertEqual(calls["count"], 1)
        self.assertEqual(diagnostics["cache"]["expensive"], {"hits": 1, "misses": 1})
        self.assertIn("expensive calculation", diagnostics["stage_timings_ms"])

    def test_diagnostics_do_not_include_sensitive_paths_or_secret_words(self):
        with performance_context({"kind": "year_to_date", "start": "2026-01-01", "end": "2026-07-14"}) as context:
            with stage("database connection/setup"):
                pass

        payload = context.as_dict()
        serialized = str(payload).lower()
        self.assertIn("total_generation_ms", payload)
        self.assertNotIn("/users/", serialized)
        self.assertNotIn("cookie", serialized)
        self.assertNotIn("token", serialized)
        self.assertNotIn("password", serialized)

    def test_bounded_html_cache_evicts_old_entries(self):
        cache = BoundedHtmlCache(max_entries=2)
        cache.set("a", "A")
        cache.set("b", "B")
        cache.get("a")
        cache.set("c", "C")

        self.assertEqual(cache.get("a"), "A")
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("c"), "C")

    def test_database_signature_changes_after_new_reading(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "power.sqlite"
            original = report.DB_PATH
            report.DB_PATH = db_path
            try:
                with sqlite3.connect(db_path) as conn:
                    self.create_readings_table(conn)
                    DataEngine(connection=conn).validate_database()
                    self.insert_reading(conn, "2026-07-13T00:00:00")

                before = report.database_content_signature()

                with sqlite3.connect(db_path) as conn:
                    self.insert_reading(conn, "2026-07-14T00:00:00")

                after = report.database_content_signature()
            finally:
                report.DB_PATH = original

        self.assertNotEqual(before, after)
        self.assertEqual(after["readings"]["latest"], "2026-07-14T00:00:00")

    def test_ev_session_source_start_index_exists(self):
        with sqlite3.connect(":memory:") as conn:
            self.create_readings_table(conn)
            DataEngine(connection=conn).validate_database()
            indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list(ev_sessions)").fetchall()
            }
        self.assertIn("idx_ev_sessions_household_source_start", indexes)

    def test_last_12_month_benchmark_maps_to_custom_range(self):
        settings = report.load_settings()
        selected, start, end = resolve_benchmark_period(
            "last_12_months",
            settings,
            date(2026, 7, 14),
            None,
            None,
        )

        self.assertEqual(selected.kind, "custom")
        self.assertEqual(start, "2025-07-15")
        self.assertEqual(end, "2026-07-14")

    @staticmethod
    def create_readings_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE readings (
                measurement_time TEXT PRIMARY KEY,
                tariff_bucket TEXT,
                production_kwh REAL,
                solar_to_home_kwh REAL,
                solar_to_grid_kwh REAL,
                grid_to_home_kwh REAL,
                grid_to_home_w REAL,
                import_cost REAL
            )
            """
        )
        conn.commit()

    @staticmethod
    def insert_reading(conn: sqlite3.Connection, timestamp: str) -> None:
        conn.execute(
            """
            INSERT INTO readings (
                household_id, measurement_time, tariff_bucket, production_kwh,
                solar_to_home_kwh, solar_to_grid_kwh, grid_to_home_kwh
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("default_household", timestamp, "off_peak", 1.0, 0.4, 0.6, 0.3),
        )
        conn.commit()


if __name__ == "__main__":
    unittest.main()
