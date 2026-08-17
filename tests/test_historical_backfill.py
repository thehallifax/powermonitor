import unittest
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.historical_backfill import build_backfill_preview, most_recent_completed_au_summer
from engine.data_engine import DataEngine
import backfill_solaredge


class HistoricalBackfillTests(unittest.TestCase):
    def test_previous_summer_uses_most_recent_completed_au_summer(self):
        start, end = most_recent_completed_au_summer(date(2026, 7, 13))

        self.assertEqual(start, date(2025, 12, 1))
        self.assertEqual(end, date(2026, 3, 31))

    def test_preview_does_not_treat_missing_intervals_as_battery_ready(self):
        coverage = {
            "providers": {
                "solaredge": {
                    "days": {
                        "2025-12-01": {
                            "first": "2025-12-01T00:00:00+08:00",
                            "last": "2025-12-01T23:45:00+08:00",
                            "battery_suitable": False,
                        }
                    }
                }
            }
        }

        preview = build_backfill_preview(
            coverage,
            today=date(2026, 7, 13),
            preset="previous_summer",
        )

        self.assertEqual(preview["range"]["start"], "2025-12-01")
        self.assertEqual(preview["range"]["end"], "2026-03-31")
        self.assertFalse(preview["battery_suitability"]["suitable"])
        self.assertEqual(preview["existing_coverage"]["days_with_any_reading"], 1)

    def test_backfill_authentication_failure_marks_job_failed(self):
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "power.sqlite"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                create_readings(conn)
            DataEngine(db_path=db_path).create_historical_backfill_job(
                job_id="job-1",
                provider_id="solaredge",
                site_id="default",
                range_preset="custom",
                start_date=date(2026, 7, 11),
                end_date=date(2026, 7, 12),
                resolution="interval_15m",
                quality_tier="interval_grade",
                data_types=["solar_generation"],
                progress={"chunks_total": 2, "chunks_completed": 0},
                status="queued",
            )
            outfile = Path(directory) / "backfill.json"
            with patch.object(backfill_solaredge, "DB_PATH", db_path), patch.object(
                backfill_solaredge,
                "OUTFILE",
                outfile,
            ), patch.object(
                backfill_solaredge,
                "fetch_with_session_recovery",
                side_effect=backfill_solaredge.AuthenticationFailure("Credentials missing."),
            ), patch.object(sys, "argv", ["backfill_solaredge.py", "--resume", "--job-id", "job-1"]):
                result = backfill_solaredge.run()

            job = DataEngine(db_path=db_path).get_historical_backfill_job("job-1")
            progress = job["progress"]
            self.assertEqual(result, 1)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(progress["chunks_completed"], 2)
            self.assertEqual(progress["failed_days"], ["2026-07-11", "2026-07-12"])
            self.assertIn("Credentials missing", " ".join(job["warnings"]))

    def test_backfill_regenerates_report_once_after_successful_completion(self):
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "power.sqlite"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                create_readings(conn)
            outfile = Path(directory) / "backfill.json"

            def fake_fetch(day, output_path):
                output_path.write_text("{}", encoding="utf-8")
                return "ok", 96, None

            with patch.object(backfill_solaredge, "DB_PATH", db_path), patch.object(
                backfill_solaredge,
                "OUTFILE",
                outfile,
            ), patch.object(
                backfill_solaredge,
                "fetch_with_session_recovery",
                side_effect=fake_fetch,
            ), patch.object(
                backfill_solaredge,
                "payload_diagnostics",
                return_value={"measurement_count": 96},
            ), patch.object(
                backfill_solaredge,
                "import_day",
                return_value=(96, {"imported_or_updated": 96, "data_received": 96, "duplicates_skipped": 0, "zero_payload_skipped": 0}, "Imported 96 readings"),
            ), patch.object(backfill_solaredge, "generate_report") as generate_report, patch.object(
                sys,
                "argv",
                [
                    "backfill_solaredge.py",
                    "--range",
                    "custom",
                    "--start",
                    "2026-07-11",
                    "--end",
                    "2026-07-12",
                    "--job-id",
                    "job-1",
                ],
            ):
                result = backfill_solaredge.run()

            job = DataEngine(db_path=db_path).get_historical_backfill_job("job-1")
            progress = job["progress"]
            self.assertEqual(result, 0)
            self.assertEqual(job["status"], "complete")
            self.assertEqual(progress["chunks_completed"], 2)
            self.assertEqual(progress["imported_or_updated"], 192)
            generate_report.assert_called_once()


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
