import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import report
import web
from engine.data_engine import DataEngine


class SimplifiedWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "power.sqlite"
        self.settings_path = Path(self.temp.name) / "settings.json"
        self.settings_path.write_text(json.dumps({"current_plan": "synergy_ev_add_on"}), encoding="utf-8")
        self.original = (report.DB_PATH, report.SETTINGS_PATH, report.HOUSEHOLD_TIMELINE_PATH)
        report.DB_PATH = self.db_path
        report.SETTINGS_PATH = self.settings_path
        report.HOUSEHOLD_TIMELINE_PATH = self.settings_path.parent / "household_timeline.json"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """CREATE TABLE readings (
                    household_id TEXT DEFAULT 'default_household', measurement_time TEXT,
                    tariff_bucket TEXT, production_kwh REAL, solar_to_home_kwh REAL,
                    solar_to_grid_kwh REAL, grid_to_home_w REAL, grid_to_home_kwh REAL,
                    import_cost REAL)"""
            )
            DataEngine(connection=conn).validate_database()
            conn.execute(
                "INSERT INTO readings (household_id, measurement_time, tariff_bucket, production_kwh, solar_to_home_kwh, solar_to_grid_kwh, grid_to_home_w, grid_to_home_kwh, import_cost) VALUES ('default_household', '2026-09-02T10:00:00', 'off_peak', 0, 0, 0, 4000, 1, 0)"
            )
            conn.execute(
                "INSERT INTO ev_sessions (household_id, transaction_id, start_time, end_time, energy_kwh, source, duration_minutes) VALUES ('default_household', 'session-1', '2026-09-02T10:00:00', '2026-09-02T11:00:00', 4, 'ocular_csv', 60)"
            )
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.DashboardHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        report.DB_PATH, report.SETTINGS_PATH, report.HOUSEHOLD_TIMELINE_PATH = self.original
        self.temp.cleanup()

    def test_dashboard_uses_period_summary_and_simplified_navigation(self):
        with urlopen(self.base + "/?period=custom&start=2026-09-02&end=2026-09-02") as response:
            body = response.read().decode("utf-8")
        self.assertIn("Household electricity", body)
        self.assertIn("1.00 kWh", body)
        self.assertIn("Dashboard", body)
        self.assertIn("EV Sessions", body)
        self.assertIn("Settings", body)
        self.assertNotIn("Recommendations", body)

        with urlopen(self.base + "/api/period-summary?period=custom&start=2026-09-02&end=2026-09-02") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["household"]["grid_import_kwh"], 1.0)
        self.assertEqual(payload["ev"]["energy_kwh"], 4.0)

    def test_ev_and_settings_pages_are_available(self):
        with urlopen(self.base + "/ev?period=custom&start=2026-09-02&end=2026-09-02") as response:
            ev_body = response.read().decode("utf-8")
        with urlopen(self.base + "/settings") as response:
            settings_body = response.read().decode("utf-8")
        self.assertIn("Session history", ev_body)
        self.assertIn("Import Ocular sessions", ev_body)
        self.assertIn("Synergy tariff history", settings_body)

    def test_future_tariff_addition_preserves_the_previous_period(self):
        first = Request(
            self.base + "/settings/tariff/add",
            data=urlencode({"effective_from": "2026-10-01", "plan_id": "synergy_home_plan"}).encode(),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(first) as response:
            self.assertIn("Historical periods were preserved", response.read().decode("utf-8"))

        timeline = json.loads(report.HOUSEHOLD_TIMELINE_PATH.read_text(encoding="utf-8"))
        periods = [event for event in timeline["events"] if event["event_type"] == "tariff_period"]
        self.assertEqual(len(periods), 2)
        self.assertEqual(periods[0]["effective_to"], "2026-09-30")
        self.assertEqual(periods[1]["effective_from"], "2026-10-01")


if __name__ == "__main__":
    unittest.main()
