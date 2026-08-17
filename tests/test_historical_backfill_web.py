import http.client
import json
import sqlite3
from contextlib import closing
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Optional
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
                        "status": "failed",
                        "start_date": "2026-07-01",
                        "end_date": "2026-07-02",
                        "progress": {"chunks_completed": 1, "chunks_total": 2},
                    },
                    {
                        "job_id": "job-2",
                        "status": "running",
                        "start_date": "2026-07-03",
                        "end_date": "2026-07-05",
                        "progress": {"chunks_completed": 2, "chunks_total": 3},
                    }
                ],
            }
        )

        self.assertIn('method="post" action="/api/historical-backfill/start"', html)
        self.assertIn('method="post" action="/api/historical-backfill/resume"', html)
        self.assertIn('method="post" action="/api/historical-backfill/cancel"', html)
        self.assertIn('/api/historical-backfill/diagnostics?job_id=job-1', html)
        self.assertIn('/api/historical-backfill/diagnostics?job_id=job-2', html)

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
                jobs = DataEngine(db_path=context.db_path).get_historical_backfill_jobs()

        self.assertEqual(response.status, 303)
        self.assertTrue(response.headers["Location"].endswith("#historical-coverage"))
        query = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertEqual(query["historical_backfill_action"], ["start"])
        self.assertEqual(query["historical_backfill_status"], ["success"])
        self.assertEqual(query["historical_backfill_range"], ["2026-07-01 to 2026-07-02"])
        self.assertIn("Historical data import started.", query["historical_backfill_message"][0])
        popen.assert_called_once()
        self.assertIn("--resume", popen.call_args.args[0])
        self.assertEqual(len(query["historical_backfill_job_id"][0]), 32)
        regenerate.assert_called_once()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "queued")

    def test_post_resume_redirects_with_message_and_regenerates_report(self):
        with running_server(seed_job=True, job_status="failed") as context:
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

    def test_previous_summer_preset_creates_queued_job(self):
        with running_server() as context:
            with patch.object(web.subprocess, "Popen"), patch.object(web, "regenerate_dashboard_report"):
                response = context.request(
                    "POST",
                    "/api/historical-backfill/start",
                    body="provider_id=solaredge&range_preset=previous_summer",
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                )

        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["historical_backfill"]["requested_range"]["start"], "2025-12-01")
        self.assertEqual(payload["historical_backfill"]["requested_range"]["end"], "2026-03-31")

    def test_invalid_custom_dates_return_400(self):
        with running_server() as context:
            response = context.request(
                "POST",
                "/api/historical-backfill/start",
                body="provider_id=solaredge&range_preset=custom&start=2026-07-03&end=2026-07-01",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )

        self.assertEqual(response.status, 400)
        self.assertIn("on or after start", response.body)

    def test_unknown_provider_returns_400(self):
        with running_server() as context:
            response = context.request(
                "POST",
                "/api/historical-backfill/start",
                body="provider_id=fronius&range_preset=previous_summer",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )

        self.assertEqual(response.status, 400)
        self.assertIn("SolarEdge only", response.body)

    def test_unknown_resume_job_returns_404(self):
        with running_server() as context:
            response = context.request(
                "POST",
                "/api/historical-backfill/resume",
                body="job_id=missing",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )

        self.assertEqual(response.status, 404)
        self.assertIn("job not found", response.body)

    def test_non_resumable_job_returns_409(self):
        with running_server(seed_job=True, job_status="complete") as context:
            response = context.request(
                "POST",
                "/api/historical-backfill/resume",
                body="job_id=job-1",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )

        self.assertEqual(response.status, 409)
        self.assertIn("cannot be resumed", response.body)

    def test_unknown_cancel_job_returns_404(self):
        with running_server() as context:
            response = context.request(
                "POST",
                "/api/historical-backfill/cancel",
                body="job_id=missing",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )

        self.assertEqual(response.status, 404)
        self.assertIn("job not found", response.body)

    def test_start_route_does_not_block_for_long_import(self):
        def slow_popen(*args, **kwargs):
            time.sleep(0.05)
            return object()

        with running_server() as context:
            with patch.object(web.subprocess, "Popen", side_effect=slow_popen), patch.object(web, "regenerate_dashboard_report"):
                started = time.monotonic()
                response = context.request(
                    "POST",
                    "/api/historical-backfill/start",
                    body="provider_id=solaredge&range_preset=last_12_months",
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                )
                elapsed = time.monotonic() - started

        self.assertEqual(response.status, 200)
        self.assertLess(elapsed, 1.0)

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

    def test_status_endpoint_returns_safe_running_progress_fields(self):
        with running_server(seed_job=True, job_progress={"chunks_total": 10, "chunks_completed": 4, "completed_days": ["2026-07-01", "2026-07-02"], "failed_days": ["2026-07-03"], "duplicates_skipped": 5, "imported_or_updated": 96, "current_day": "2026-07-04"}) as context:
            response = context.request("GET", "/api/historical-backfill/status", headers={"Accept": "application/json"})

        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)["historical_backfill"]
        job = payload["jobs"][0]
        self.assertTrue(payload["polling"]["active"])
        self.assertEqual(job["job_id"], "job-1")
        self.assertEqual(job["provider_id"], "solaredge")
        self.assertEqual(job["provider"], "SolarEdge")
        self.assertEqual(job["chunks_completed"], 4)
        self.assertEqual(job["chunks_total"], 10)
        self.assertEqual(job["percentage"], 40.0)
        self.assertEqual(job["days_completed"], 4)
        self.assertEqual(job["total_days"], 10)
        self.assertEqual(job["days_remaining"], 6)
        self.assertEqual(job["failed_day_count"], 1)
        self.assertEqual(job["readings_added"], 96)
        self.assertEqual(job["imported_or_updated"], 96)
        self.assertEqual(job["duplicates_skipped"], 5)
        self.assertEqual(job["current_day"], "2026-07-04")
        self.assertFalse(job["terminal"])
        self.assertIn("estimated_finish_time", job)
        self.assertNotIn("diagnostics", job)
        self.assertNotIn("warnings", job)
        self.assertNotIn("site_id", job)
        self.assertNotIn("household_id", job)
        self.assertNotIn("completed_days", job)
        self.assertNotIn("failed_days", job)

    def test_status_endpoint_reflects_persisted_changes_without_report_regeneration(self):
        with running_server(seed_job=True, job_progress={"chunks_total": 366, "chunks_completed": 2, "completed_days": ["2025-07-13"], "current_day": "2025-07-14"}) as context:
            first = context.request("GET", "/api/historical-backfill/status?job_id=job-1", headers={"Accept": "application/json"})
            with closing(sqlite3.connect(context.db_path)) as conn, conn:
                conn.execute(
                    """
                    UPDATE historical_backfill_jobs
                    SET status = 'complete',
                        progress_json = ?,
                        completed_at = ?,
                        updated_at = ?
                    WHERE job_id = 'job-1'
                    """,
                    (
                        json.dumps(
                            {
                                "chunks_total": 366,
                                "chunks_completed": 366,
                                "completed_days": ["2025-07-13", "2026-07-13"],
                                "data_received": 35136,
                                "imported_or_updated": 31393,
                                "duplicates_skipped": 3743,
                                "failed_days": [],
                                "current_day": "2026-07-13",
                            },
                            sort_keys=True,
                        ),
                        "2026-07-13T21:56:53+08:00",
                        "2026-07-13T21:56:53+08:00",
                    ),
                )
                conn.commit()
            second = context.request("GET", "/api/historical-backfill/status?job_id=job-1", headers={"Accept": "application/json"})

        first_job = json.loads(first.body)["historical_backfill"]["job"]
        second_job = json.loads(second.body)["historical_backfill"]["job"]
        self.assertEqual(first_job["chunks_completed"], 2)
        self.assertEqual(second_job["chunks_completed"], 366)
        self.assertEqual(second_job["percentage"], 100.0)
        self.assertEqual(second_job["imported_or_updated"], 31393)
        self.assertEqual(second_job["duplicates_skipped"], 3743)
        self.assertTrue(second_job["terminal"])

    def test_jobs_endpoint_marks_terminal_jobs_and_stops_polling(self):
        with running_server(seed_job=True, job_status="complete", job_progress={"chunks_total": 2, "chunks_completed": 2, "completed_days": ["2026-07-01", "2026-07-02"]}) as context:
            response = context.request("GET", "/api/historical-backfill/status?job_id=job-1", headers={"Accept": "application/json"})

        payload = json.loads(response.body)["historical_backfill"]
        job = payload["jobs"][0]
        self.assertFalse(payload["polling"]["active"])
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["percentage_complete"], 100.0)
        self.assertEqual(job["summary_message"], "Historical import complete")

    def test_jobs_endpoint_surfaces_failed_progress(self):
        with running_server(seed_job=True, job_status="failed", job_progress={"chunks_total": 3, "chunks_completed": 3, "completed_days": ["2026-07-01"], "failed_days": ["2026-07-02", "2026-07-03"]}) as context:
            response = context.request("GET", "/api/historical-backfill/status", headers={"Accept": "application/json"})

        job = json.loads(response.body)["historical_backfill"]["jobs"][0]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["failed_day_count"], 2)
        self.assertEqual(job["last_successful_day"], "2026-07-01")

    def test_status_endpoint_surfaces_cancelled_terminal_state(self):
        with running_server(seed_job=True, job_status="cancelled", job_progress={"chunks_total": 3, "chunks_completed": 1, "completed_days": ["2026-07-01"]}) as context:
            response = context.request("GET", "/api/historical-backfill/status", headers={"Accept": "application/json"})

        payload = json.loads(response.body)["historical_backfill"]
        job = payload["jobs"][0]
        self.assertFalse(payload["polling"]["active"])
        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(job["terminal"])

    def test_status_endpoint_includes_live_coverage_summary_without_day_maps(self):
        with running_server(seed_job=True, job_status="complete") as context:
            with closing(sqlite3.connect(context.db_path)) as conn, conn:
                insert_reading(conn, "2025-12-01T00:15:00+08:00", production=1.0, grid_import=0.1, grid_export=0.2)
                insert_reading(conn, "2026-07-13T00:15:00+08:00", production=1.0, grid_import=0.1, grid_export=0.2)
            response = context.request("GET", "/api/historical-backfill/status", headers={"Accept": "application/json"})

        coverage = json.loads(response.body)["historical_backfill"]["coverage"]
        self.assertEqual(coverage["interval_grade_days"], 2)
        self.assertEqual(coverage["battery_ready_days"], 2)
        self.assertEqual(coverage["previous_summer"]["days_available"], 1)
        self.assertIn("coverage_by_season", coverage)
        self.assertNotIn("days", coverage)
        self.assertNotIn("providers", coverage)

    def test_public_diagnostics_uses_bounded_payload(self):
        with running_server(seed_job=True, job_status="failed", job_progress={"chunks_total": 10, "chunks_completed": 10, "completed_days": ["2026-07-01", "2026-07-02"], "failed_days": [f"2026-07-{day:02d}" for day in range(3, 10)]}) as context:
            response = context.request("GET", "/api/historical-backfill/diagnostics?job_id=job-1", headers={"Accept": "application/json"})

        payload = json.loads(response.body)["historical_backfill"]
        job = payload["job"]
        self.assertEqual(job["failed_day_count"], 7)
        self.assertLessEqual(len(job["failed_day_sample"]), 3)
        serialized = json.dumps(payload)
        self.assertNotIn('"failed_days"', serialized)
        self.assertNotIn('"completed_days"', serialized)
        self.assertNotIn('"days": {', json.dumps(payload))

    def test_historical_section_includes_polling_markup_and_same_origin_script(self):
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
                        "end_date": "2026-07-10",
                        "progress": {"chunks_completed": 4, "chunks_total": 10, "current_day": "2026-07-05"},
                    }
                ],
            }
        )

        self.assertIn('id="historical-backfill-jobs"', html)
        self.assertIn('data-endpoint="/api/historical-backfill/status"', html)
        self.assertIn('data-poll-active="true"', html)
        self.assertIn('data-poll-interval-ms="5000"', html)
        self.assertIn("Current day: 2026-07-05", html)
        self.assertIn("40% complete", html)

    def test_historical_section_marks_terminal_jobs_as_not_polling(self):
        html = report.historical_coverage_html(
            {
                "historical_coverage": {"providers": {}, "quality_tiers": {}},
                "seasonal_history": {"range": {}, "existing_coverage": {}, "gaps": {}},
                "solaredge_capability_audit": {},
                "historical_backfill_jobs": [
                    {
                        "job_id": "job-1",
                        "status": "failed",
                        "start_date": "2026-07-01",
                        "end_date": "2026-07-03",
                        "progress": {"chunks_completed": 3, "chunks_total": 3, "failed_days": ["2026-07-02"]},
                    }
                ],
            }
        )

        self.assertIn('data-poll-active="false"', html)

    def test_dashboard_asset_and_csp_allow_same_origin_polling_without_meta_refresh(self):
        with running_server(seed_job=True) as context:
            root = context.request("GET", "/", headers={"Accept": "text/html"})
            script = context.request("GET", "/assets/dashboard.js", headers={"Accept": "application/javascript"})

        self.assertEqual(script.status, 200)
        self.assertIn("historical-backfill-jobs", script.body)
        self.assertIn("updateCoverage", script.body)
        self.assertIn("script-src 'self'", root.headers["Content-Security-Policy"])
        self.assertNotIn("script-src 'unsafe-inline'", root.headers["Content-Security-Policy"])
        self.assertNotIn("Refresh", root.headers)


class running_server:
    def __init__(self, *, seed_job: bool = False, job_status: str = "running", provider_id: str = "solaredge", job_progress: Optional[dict] = None):
        self.seed_job = seed_job
        self.job_status = job_status
        self.provider_id = provider_id
        self.job_progress = job_progress or {"chunks_total": 2, "chunks_completed": 1, "completed_days": ["2026-07-01"], "duplicates_skipped": 3}

    def __enter__(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "power.sqlite"
        self.original_db_path = report.DB_PATH
        report.DB_PATH = self.db_path
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            create_readings(conn)
            DataEngine(connection=conn).validate_database()
        if self.seed_job:
            job = DataEngine(db_path=self.db_path).create_historical_backfill_job(
                job_id="job-1",
                provider_id=self.provider_id,
                site_id="default",
                range_preset="custom",
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 2),
                resolution="interval_15m",
                quality_tier="interval_grade",
                data_types=["solar_generation"],
                progress=self.job_progress,
                status=self.job_status,
            )
            created_at = datetime.now().astimezone() - timedelta(minutes=15)
            updated_at = created_at + timedelta(minutes=10)
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute(
                    """
                    UPDATE historical_backfill_jobs
                    SET created_at = ?, updated_at = ?, completed_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        created_at.isoformat(timespec="seconds"),
                        updated_at.isoformat(timespec="seconds"),
                        updated_at.isoformat(timespec="seconds") if self.job_status in {"complete", "failed", "cancelled"} else None,
                        job["job_id"],
                    ),
                )
                conn.commit()
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


def insert_reading(conn, measurement_time: str, *, production: float, grid_import: float, grid_export: float):
    conn.execute(
        """
        INSERT INTO readings (
            household_id,
            measurement_time,
            tariff_bucket,
            production_kwh,
            solar_to_home_kwh,
            solar_to_grid_kwh,
            grid_to_home_w,
            grid_to_home_kwh,
            import_cost,
            provider_id,
            source_id,
            source_resolution
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "default_household",
            measurement_time,
            "super_off_peak",
            production,
            max(production - grid_export, 0),
            grid_export,
            grid_import * 4000,
            grid_import,
            0.0,
            "solaredge",
            "solaredge_dashboard_power",
            "interval_15m",
        ),
    )


if __name__ == "__main__":
    unittest.main()
