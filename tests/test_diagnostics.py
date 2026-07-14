import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import diagnostics
import report


class DiagnosticsTests(unittest.TestCase):
    def test_canonical_report_path_comes_from_report_module(self):
        original = report.REPORT_PATH
        try:
            with tempfile.TemporaryDirectory() as temporary:
                report.REPORT_PATH = Path(temporary) / "reports" / "index.html"
                self.assertEqual(diagnostics.canonical_report_path(), report.REPORT_PATH.resolve())
        finally:
            report.REPORT_PATH = original

    def test_live_hash_matches_report_when_root_serves_same_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.html"
            path.write_bytes(b"<html>same</html>")

            result = diagnostics.live_hash_matches_report(
                report_path=path,
                host="0.0.0.0",
                port=8765,
                fetcher=lambda _url: b"<html>same</html>",
            )

        self.assertTrue(result["matches"])
        self.assertEqual(result["report_hash"], result["live_hash"])
        self.assertEqual(result["url"], "http://127.0.0.1:8765/")

    def test_stale_report_detection_uses_threshold(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.html"
            path.write_text("old", encoding="utf-8")
            old = time.time() - 60 * 31
            os.utime(path, (old, old))

            result = diagnostics.stale_report(path, threshold_minutes=20)

        self.assertTrue(result["stale"])
        self.assertGreaterEqual(result["age_minutes"], 30)

    def test_duplicate_process_detection_groups_by_role(self):
        rows = [
            {"pid": 1, "command": "python collector/web.py"},
            {"pid": 2, "command": "python /tmp/PowerMonitor/collector/web.py"},
            {"pid": 3, "command": "python collector/collect_today.py"},
        ]

        result = diagnostics.duplicate_processes(rows)

        self.assertEqual(result["duplicate_roles"], ["web"])
        self.assertEqual(len(result["duplicates"]["web"]), 2)

    def test_diagnostics_payload_reports_expected_sections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "reports" / "index.html"
            settings_path = root / "config" / "settings.json"
            db_path = root / "data" / "power.sqlite"
            report_path.parent.mkdir(parents=True)
            settings_path.parent.mkdir(parents=True)
            db_path.parent.mkdir(parents=True)
            report_path.write_bytes(b"dashboard")
            settings_path.write_text("{}", encoding="utf-8")
            db_path.write_bytes(b"sqlite")

            original_paths = (report.REPORT_PATH, report.SETTINGS_PATH, report.DB_PATH)
            report.REPORT_PATH, report.SETTINGS_PATH, report.DB_PATH = report_path, settings_path, db_path
            try:
                with patch("diagnostics.validate_launch_agents.validate", return_value={"issues": [], "loaded_services": ["com.local.powermonitor.web"]}), \
                     patch("diagnostics.duplicate_processes", return_value={"processes": [], "duplicates": {}, "duplicate_roles": []}), \
                     patch("diagnostics.duplicate_listeners", return_value={"port": 8765, "listeners": [], "duplicate": False, "count": 0}), \
                     patch("diagnostics.plist_runtime_mismatches", return_value={"wrong_working_directory": [], "mismatched_executable": []}), \
                     patch("diagnostics.resolve_web_bind", return_value=("127.0.0.1", 8765)):
                    payload = diagnostics.diagnostics_payload(fetcher=lambda _url: b"dashboard")
            finally:
                report.REPORT_PATH, report.SETTINGS_PATH, report.DB_PATH = original_paths

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["canonical_report_path"], str(report_path.resolve()))
        self.assertTrue(payload["live_report_hash"]["matches"])
        self.assertEqual(payload["loaded_launch_agents"], ["com.local.powermonitor.web"])
        self.assertIn("battery_catalogue", payload)
        self.assertEqual(payload["battery_catalogue"]["validation_status"], "valid")

    def test_settings_runtime_diagnostics_rendering(self):
        html = report.runtime_check_html(
            {
                "git_commit": "abc1234",
                "python_version": "3.14.0",
                "executable": "/usr/bin/python3",
                "working_directory": "/tmp/PowerMonitor",
                "running_in_project_venv": False,
                "playwright_available": True,
                "playwright_chromium_available": True,
                "pypdf_available": True,
                "pdftotext_available": False,
                "project_root": "/tmp/PowerMonitor",
                "report_path": "/tmp/PowerMonitor/reports/index.html",
                "report_generation_timestamp": "2026-07-13T10:00:00+08:00",
                "database_path": "/tmp/PowerMonitor/data/power.sqlite",
                "settings_path": "/tmp/PowerMonitor/config/settings.json",
                "web_bind_address": "0.0.0.0:8765",
                "active_tariff_version": {"version_id": "Synergy:EV:v2026"},
                "runtime_health_summary": {"status": "ok"},
                "active_launch_agent_labels": ["com.local.powermonitor.web"],
                "warnings": [],
            }
        )

        self.assertIn("Runtime diagnostics", html)
        self.assertIn("Git commit", html)
        self.assertIn("Report path", html)
        self.assertIn("Synergy:EV:v2026", html)
        self.assertIn("com.local.powermonitor.web", html)


if __name__ == "__main__":
    unittest.main()
