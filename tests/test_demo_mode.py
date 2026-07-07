import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import collector_health
import demo_mode
import report
from engine.data_engine import DataEngine


class DemoModeTests(unittest.TestCase):
    def test_demo_environment_generates_separate_fictional_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = demo_mode.ensure_demo_environment(root, today=date(2026, 7, 7))

            self.assertTrue(env.db_path.exists())
            self.assertTrue(env.settings_path.exists())
            self.assertTrue(env.home_profile_path.exists())
            self.assertTrue(env.weather_path.exists())
            self.assertTrue(env.recommendations_path.exists())
            self.assertIn("data/demo", env.db_path.as_posix())
            self.assertIn("config/demo", env.home_profile_path.as_posix())

            profile = json.loads(env.home_profile_path.read_text(encoding="utf-8"))
            weather = json.loads(env.weather_path.read_text(encoding="utf-8"))
            recommendations = json.loads(env.recommendations_path.read_text(encoding="utf-8"))

            self.assertTrue(profile["demo"])
            self.assertEqual(profile["location_region"], "WA")
            self.assertEqual(weather["source"], "fictional_demo_weather")
            self.assertTrue(recommendations["items"])

            with sqlite3.connect(env.db_path) as conn:
                DataEngine(connection=conn).validate_database()
                readings = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
                sessions = conn.execute("SELECT COUNT(*) FROM ev_sessions").fetchone()[0]
                bills = conn.execute("SELECT COUNT(*) FROM synergy_bills").fetchone()[0]
                timeline = conn.execute("SELECT COUNT(*) FROM timeline_entries").fetchone()[0]

        self.assertGreater(readings, 0)
        self.assertGreater(sessions, 0)
        self.assertEqual(bills, 2)
        self.assertGreaterEqual(timeline, 3)

    def test_report_can_render_dashboard_from_demo_mode_without_providers(self):
        state = capture_report_state()
        collector_state = (collector_health.STATE_PATH, collector_health.SETTINGS_PATH)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                report.PROJECT_ROOT = Path(temp_dir)
                report.activate_demo_mode(today=date(2026, 7, 7))

                payload = report.dashboard_data(today=date(2026, 7, 7))
                html = report.build_live_html()

            self.assertTrue(payload["demo"]["enabled"])
            self.assertEqual(payload["demo"]["weather"]["source"], "fictional_demo_weather")
            self.assertGreater(payload["summary"]["grid_import_kwh"], 0)
            self.assertTrue(payload["recommendations"])
            self.assertIn("PowerMonitor", html)
        finally:
            restore_report_state(state)
            collector_health.STATE_PATH, collector_health.SETTINGS_PATH = collector_state

    def test_demo_requested_accepts_flag_or_environment(self):
        self.assertTrue(demo_mode.demo_requested(["collector/report.py", "--demo"], {}))
        self.assertTrue(demo_mode.demo_requested(["collector/report.py"], {"POWMONITOR_DEMO": "1"}))
        self.assertFalse(demo_mode.demo_requested(["collector/report.py"], {}))


def capture_report_state():
    return {
        "PROJECT_ROOT": report.PROJECT_ROOT,
        "DB_PATH": report.DB_PATH,
        "REPORT_PATH": report.REPORT_PATH,
        "LOG_PATH": report.LOG_PATH,
        "SETTINGS_PATH": report.SETTINGS_PATH,
        "HOME_PROFILE_PATH": report.HOME_PROFILE_PATH,
        "COLLECT_LOG_PATH": report.COLLECT_LOG_PATH,
        "DEMO_MODE": report.DEMO_MODE,
        "DEMO_WEATHER_PATH": report.DEMO_WEATHER_PATH,
        "DEMO_RECOMMENDATIONS_PATH": report.DEMO_RECOMMENDATIONS_PATH,
    }


def restore_report_state(state):
    for key, value in state.items():
        setattr(report, key, value)


if __name__ == "__main__":
    unittest.main()
