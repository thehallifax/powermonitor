import json
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import collector.import_solaredge as import_solaredge


class SolarEdgeImportTests(unittest.TestCase):
    def run_import(self, db_path: Path, json_path: Path) -> str:
        original_db_path = import_solaredge.DB_PATH
        original_argv = sys.argv
        import_solaredge.DB_PATH = db_path
        try:
            sys.argv = ["import_solaredge.py", str(json_path)]
            output = StringIO()
            with redirect_stdout(output):
                import_solaredge.main()
            return output.getvalue()
        finally:
            import_solaredge.DB_PATH = original_db_path
            sys.argv = original_argv

    def test_import_summary_reports_range_records_duplicates_and_promotions(self):
        fixture = {
            "measurements": [
                {
                    "measurementTime": "2026-06-30T09:00:00+08:00",
                    "production": 1000,
                    "consumption": 500,
                    "productionDistribution": {"productionToHome": 400, "productionToGrid": 600},
                    "consumptionDistribution": {"consumptionFromGrid": 100},
                },
                {
                    "measurementTime": "2026-06-30T09:15:00+08:00",
                    "production": 1200,
                    "consumption": 700,
                    "productionDistribution": {"productionToHome": 500, "productionToGrid": 700},
                    "consumptionDistribution": {"consumptionFromGrid": 200},
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            temp_dir = Path(temporary)
            db_path = temp_dir / "power.sqlite"
            json_path = temp_dir / "solaredge.json"
            json_path.write_text(json.dumps(fixture), encoding="utf-8")
            first_output = self.run_import(db_path, json_path)
            second_output = self.run_import(db_path, json_path)

            with closing(sqlite3.connect(db_path)) as conn, conn:
                count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

        self.assertEqual(count, 2)
        self.assertIn("data received 2", first_output)
        self.assertIn("time range imported 2026-06-30T09:00:00+08:00 to 2026-06-30T09:15:00+08:00", first_output)
        self.assertIn("records imported 2", first_output)
        self.assertIn("duplicates ignored 2", second_output)
        self.assertIn("observations promoted 0", first_output)

    def test_import_normalizes_utc_measurements_to_awst_local_day_summary_range(self):
        fixture = {
            "measurements": [
                {
                    "measurementTime": "2026-07-02T16:15:00Z",
                    "production": 1000,
                    "consumption": 500,
                    "productionDistribution": {"productionToHome": 600, "productionToGrid": 400},
                    "consumptionDistribution": {"consumptionFromGrid": 100},
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            temp_dir = Path(temporary)
            db_path = temp_dir / "power.sqlite"
            json_path = temp_dir / "solaredge.json"
            json_path.write_text(json.dumps(fixture), encoding="utf-8")

            output = self.run_import(db_path, json_path)

            with closing(sqlite3.connect(db_path)) as conn, conn:
                measurement_time, generated, exported = conn.execute(
                    """
                    SELECT measurement_time, production_kwh, solar_to_grid_kwh
                    FROM readings
                    """
                ).fetchone()
                summary_generated, summary_exported = conn.execute(
                    """
                    SELECT COALESCE(SUM(production_kwh), 0), COALESCE(SUM(solar_to_grid_kwh), 0)
                    FROM readings
                    WHERE measurement_time >= ? AND measurement_time < ?
                    """,
                    ("2026-07-03", "2026-07-04"),
                ).fetchone()

        self.assertEqual(measurement_time, "2026-07-03T00:15:00+08:00")
        self.assertAlmostEqual(generated, 0.25)
        self.assertAlmostEqual(exported, 0.1)
        self.assertGreater(summary_generated, 0)
        self.assertGreater(summary_exported, 0)
        self.assertIn("time range imported 2026-07-03T00:15:00+08:00 to 2026-07-03T00:15:00+08:00", output)

    def test_zero_payload_does_not_overwrite_existing_non_zero_reading(self):
        non_zero = {
            "measurements": [
                {
                    "measurementTime": "2026-07-03T09:00:00+08:00",
                    "production": 1200,
                    "consumption": 700,
                    "productionDistribution": {"productionToHome": 500, "productionToGrid": 700},
                    "consumptionDistribution": {"consumptionFromGrid": 200},
                },
            ]
        }
        zero_payload = {
            "measurements": [
                {
                    "measurementTime": "2026-07-03T09:00:00+08:00",
                    "production": 0,
                    "consumption": 0,
                    "productionDistribution": {"productionToHome": 0, "productionToGrid": 0},
                    "consumptionDistribution": {"consumptionFromGrid": 0},
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            temp_dir = Path(temporary)
            db_path = temp_dir / "power.sqlite"
            first_path = temp_dir / "non-zero.json"
            second_path = temp_dir / "zero.json"
            first_path.write_text(json.dumps(non_zero), encoding="utf-8")
            second_path.write_text(json.dumps(zero_payload), encoding="utf-8")

            self.run_import(db_path, first_path)
            second_output = self.run_import(db_path, second_path)

            with closing(sqlite3.connect(db_path)) as conn, conn:
                generated, exported = conn.execute(
                    """
                    SELECT production_kwh, solar_to_grid_kwh
                    FROM readings
                    WHERE measurement_time = ?
                    """,
                    ("2026-07-03T09:00:00+08:00",),
                ).fetchone()

        self.assertAlmostEqual(generated, 0.3)
        self.assertAlmostEqual(exported, 0.175)
        self.assertIn("zero payload rows skipped 1", second_output)

    def test_non_zero_payload_repairs_existing_zero_reading(self):
        zero_payload = {
            "measurements": [
                {
                    "measurementTime": "2026-07-03T09:00:00+08:00",
                    "production": 0,
                    "consumption": 0,
                    "productionDistribution": {"productionToHome": 0, "productionToGrid": 0},
                    "consumptionDistribution": {"consumptionFromGrid": 0},
                },
            ]
        }
        non_zero = {
            "measurements": [
                {
                    "measurementTime": "2026-07-03T09:00:00+08:00",
                    "production": 1200,
                    "consumption": 700,
                    "productionDistribution": {"productionToHome": 500, "productionToGrid": 700},
                    "consumptionDistribution": {"consumptionFromGrid": 200},
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            temp_dir = Path(temporary)
            db_path = temp_dir / "power.sqlite"
            first_path = temp_dir / "zero.json"
            second_path = temp_dir / "non-zero.json"
            first_path.write_text(json.dumps(zero_payload), encoding="utf-8")
            second_path.write_text(json.dumps(non_zero), encoding="utf-8")

            self.run_import(db_path, first_path)
            second_output = self.run_import(db_path, second_path)

            with closing(sqlite3.connect(db_path)) as conn, conn:
                generated, exported = conn.execute(
                    """
                    SELECT production_kwh, solar_to_grid_kwh
                    FROM readings
                    WHERE measurement_time = ?
                    """,
                    ("2026-07-03T09:00:00+08:00",),
                ).fetchone()

        self.assertAlmostEqual(generated, 0.3)
        self.assertAlmostEqual(exported, 0.175)
        self.assertIn("records updated 1", second_output)


if __name__ == "__main__":
    unittest.main()
