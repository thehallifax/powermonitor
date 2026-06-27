import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report
import web
from engine.data_engine import DataEngine


class RecommendationLifecycleWebTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "power.sqlite"
        self.report_path = Path(self.temp_dir.name) / "index.html"
        self.collect_log_path = Path(self.temp_dir.name) / "collect.log"
        self.original_paths = (report.DB_PATH, report.REPORT_PATH, report.COLLECT_LOG_PATH)
        report.DB_PATH = self.db_path
        report.REPORT_PATH = self.report_path
        report.COLLECT_LOG_PATH = self.collect_log_path
        with sqlite3.connect(self.db_path) as conn:
            create_readings(conn)
            insert_reading(conn)
            DataEngine(connection=conn).validate_database()

    def tearDown(self):
        report.DB_PATH, report.REPORT_PATH, report.COLLECT_LOG_PATH = self.original_paths
        self.temp_dir.cleanup()

    def test_web_can_dismiss_snooze_and_restore_recommendation(self):
        server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            dashboard = urlopen(f"{base_url}/", timeout=10).read().decode("utf-8")
            self.assertIn("Recommendations", dashboard)

            post(base_url, "peak_cost_reduction", "dismiss")
            lifecycle = lifecycle_state("peak_cost_reduction", self.db_path)
            self.assertEqual(lifecycle["state"], "dismissed")

            post(base_url, "peak_cost_reduction", "snooze_1d")
            lifecycle = lifecycle_state("peak_cost_reduction", self.db_path)
            self.assertEqual(lifecycle["state"], "snoozed_until")
            self.assertIsNotNone(lifecycle["snoozed_until"])

            post(base_url, "peak_cost_reduction", "snooze_next_billing")
            lifecycle = lifecycle_state("peak_cost_reduction", self.db_path)
            self.assertEqual(lifecycle["state"], "snoozed_until")
            self.assertEqual(lifecycle["reason"], "Snoozed until next billing period")

            post(base_url, "peak_cost_reduction", "restore")
            lifecycle = lifecycle_state("peak_cost_reduction", self.db_path)
            self.assertEqual(lifecycle["state"], "active")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)


def post(base_url: str, stable_id: str, action: str) -> None:
    body = urlencode({"stable_id": stable_id, "action": action}).encode("utf-8")
    request = Request(
        f"{base_url}/recommendations/lifecycle",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response = urlopen(request, timeout=10)
    self_url = response.geturl()
    if not self_url.startswith(base_url):
        raise AssertionError(f"Unexpected redirect URL: {self_url}")


def lifecycle_state(stable_id: str, db_path: Path) -> dict:
    engine = DataEngine(db_path=db_path)
    return engine.get_recommendation_lifecycle([stable_id])[stable_id]


def create_readings(conn):
    conn.execute(
        """
        CREATE TABLE readings (
            measurement_time TEXT,
            tariff_bucket TEXT,
            production_kwh REAL,
            solar_to_home_kwh REAL,
            solar_to_grid_kwh REAL,
            grid_to_home_w REAL,
            grid_to_home_kwh REAL,
            import_cost REAL
        )
        """
    )


def insert_reading(conn):
    conn.execute(
        """
        INSERT INTO readings (
            measurement_time,
            tariff_bucket,
            production_kwh,
            solar_to_home_kwh,
            solar_to_grid_kwh,
            grid_to_home_w,
            grid_to_home_kwh,
            import_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("2026-06-27T12:00:00", "peak", 1.0, 0.4, 0.6, 400.0, 2.0, 1.0),
    )


if __name__ == "__main__":
    unittest.main()
