import http.client
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report
import web
from engine.data_engine import DataEngine


class HistoricalBackfillWebTests(unittest.TestCase):
    def test_rendered_historical_forms_use_post_and_expected_action_urls(self):
        html = report.historical_coverage_html(
            {
                "historical_coverage": {"providers": {}, "quality_tiers": {}},
                "seasonal_history": {"range": {}, "existing_coverage": {}, "gaps": {}},
                "solaredge_capability_audit": {},
                "historical_backfill_jobs": [
                    {
                        "job_id": "job-1",
                        "status": "running",
                        "start_date": "2026-07-01",
                        "end_date": "2026-07-02",
                        "progress": {"chunks_completed": 1, "chunks_total": 2},
                    }
                ],
            }
        )

        self.assertIn('method="post" action="/api/historical-backfill/start"', html)
        self.assertIn('method="post" action="/api/historical-backfill/resume"', html)
        self.assertIn('method="post" action="/api/historical-backfill/cancel"', html)
        self.assertIn('/api/historical-backfill/diagnostics?job_id=job-1', html)

    def test_post_start_redirects_with_message_and_regenerates_report_for_remote_host(self):
        with running_server() as context:
            with patch.object(web.subprocess, "Popen") as popen, patch.object(web, "regenerate_dashboard_report") as regenerate:
                response = context.request(
                    "POST",
                    "/api/historical-backfill/start",
                    body="provider_id=solaredge&range_preset=custom&start=2026-07-01&end=2026-07-02",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "text/html",
                        "Host": f"192.168.1.20:{context.port}",
                    },
                )

        self.assertEqual(response.status, 303)
        self.assertTrue(response.headers["Location"].endswith("#historical-coverage"))
        query = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertEqual(query["historical_backfill_action"], ["start"])
        self.assertEqual(query["historical_backfill_status"], ["success"])
        self.assertEqual(query["historical_backfill_range"], ["2026-07-01 to 2026-07-02"])
        self.assertIn("Historical data import started.", query["historical_backfill_message"][0])
        popen.assert_called_once()
        regenerate.assert_called_once()

    def test_post_resume_redirects_with_message_and_regenerates_report(self):
        with running_server(seed_job=True) as context:
            with patch.object(web.subprocess, "Popen") as popen, patch.object(web, "regenerate_dashboard_report") as regenerate:
                response = context.request(
                    "POST",
                    "/api/historical-backfill/resume",
                    body="job_id=job-1",
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"},
                )

        self.assertEqual(response.status, 303)
        query = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertEqual(query["historical_backfill_action"], ["resume"])
        self.assertEqual(query["historical_backfill_job_id"], ["job-1"])
        self.assertIn("resumed", query["historical_backfill_message"][0])
        popen.assert_called_once()
        regenerate.assert_called_once()

    def test_post_cancel_redirects_with_message_and_regenerates_report(self):
        with running_server(seed_job=True) as context:
            with patch.object(web, "regenerate_dashboard_report") as regenerate:
                response = context.request(
                    "POST",
                    "/api/historical-backfill/cancel",
                    body="job_id=job-1",
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"},
                )

        self.assertEqual(response.status, 303)
        query = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertEqual(query["historical_backfill_action"], ["cancel"])
        self.assertEqual(query["historical_backfill_job_id"], ["job-1"])
        self.assertIn("Cancel requested.", query["historical_backfill_message"][0])
        regenerate.assert_called_once()

    def test_invalid_get_to_start_returns_method_error_for_api_clients(self):
        with running_server() as context:
            response = context.request("GET", "/api/historical-backfill/start", headers={"Accept": "application/json"})

        self.assertEqual(response.status, 405)
        self.assertIn("method_not_allowed", response.body)
        self.assertIn("Use POST", response.body)

    def test_invalid_get_to_action_redirects_browser_safely(self):
        with running_server() as context:
            response = context.request("GET", "/api/historical-backfill/start", headers={"Accept": "text/html"})

        self.assertEqual(response.status, 303)
        self.assertTrue(response.headers["Location"].endswith("#historical-coverage"))
        query = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertEqual(query["historical_backfill_status"], ["failure"])

    def test_historical_flash_message_renders_visible_result(self):
        html = report.historical_coverage_html(
            {
                "historical_coverage": {"providers": {}, "quality_tiers": {}},
                "seasonal_history": {"range": {}, "existing_coverage": {}, "gaps": {}},
                "solaredge_capability_audit": {},
                "historical_backfill_jobs": [],
            },
            {
                "action": "start",
                "status": "success",
                "message": "Historical data import started.",
                "job_id": "job-1",
                "range": "2026-07-01 to 2026-07-02",
                "next_step": "Review the job status below.",
            },
        )

        self.assertIn("historical-backfill-flash", html)
        self.assertIn("Historical data import started.", html)
        self.assertIn("job-1", html)

    def test_diagnostics_route_returns_job_and_coverage(self):
        with running_server(seed_job=True) as context:
            response = context.request("GET", "/api/historical-backfill/diagnostics?job_id=job-1", headers={"Accept": "application/json"})

        self.assertEqual(response.status, 200)
        self.assertIn('"job_id": "job-1"', response.body)
        self.assertIn('"coverage"', response.body)


class running_server:
    def __init__(self, *, seed_job: bool = False):
        self.seed_job = seed_job

    def __enter__(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "power.sqlite"
        self.original_db_path = report.DB_PATH
        report.DB_PATH = self.db_path
        with sqlite3.connect(self.db_path) as conn:
            create_readings(conn)
            DataEngine(connection=conn).validate_database()
        if self.seed_job:
            DataEngine(db_path=self.db_path).create_historical_backfill_job(
                job_id="job-1",
                provider_id="solaredge",
                site_id="default",
                range_preset="custom",
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 2),
                resolution="interval_15m",
                quality_tier="interval_grade",
                data_types=["solar_generation"],
                progress={"chunks_total": 2, "chunks_completed": 1, "completed_days": ["2026-07-01"], "duplicates_skipped": 3},
                status="running",
            )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.DashboardHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        report.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def request(self, method, path, *, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = response.read().decode("utf-8")
        headers_out = {key: value for key, value in response.getheaders()}
        conn.close()
        return Response(response.status, headers_out, payload)


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body


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


if __name__ == "__main__":
    unittest.main()
