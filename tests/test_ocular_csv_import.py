import sqlite3
import tempfile
import unittest
from pathlib import Path

from collector.import_ocular_export import OcularCSVImporter
from engine.data_engine import DataEngine


CSV = """Transaction ID,Charging start time,Charging stop time,Charging amount (kWh),Charging duration (min)
csv-001,2026-01-01 23:00,2026-01-02 01:00,8.5,120
csv-002,2026-01-02 10:00,2026-01-02 11:00,4.25,60
"""


class OcularCsvImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "power.sqlite"
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE readings (
                    household_id TEXT DEFAULT 'default_household', measurement_time TEXT,
                    tariff_bucket TEXT, production_kwh REAL, solar_to_home_kwh REAL,
                    solar_to_grid_kwh REAL, grid_to_home_w REAL, grid_to_home_kwh REAL,
                    import_cost REAL
                )
                """
            )
            DataEngine(connection=connection).validate_database()
        self.path = Path(self.temp.name) / "ocular.csv"
        self.path.write_text(CSV, encoding="utf-8")
        self.importer = OcularCSVImporter(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_csv_is_parsed_committed_and_idempotent(self):
        self.assertTrue(self.importer.detect(self.path))
        records = self.importer.parse(self.path)
        self.assertEqual([record["record_number"] for record in records], ["csv-001", "csv-002"])
        self.assertEqual(records[0]["duration"], "120")
        context = DataEngine(db_path=self.db_path).get_household_context()

        first = self.importer.commit(records, context, job_id="job-1")
        second = self.importer.commit(records, context, job_id="job-2")

        self.assertEqual(first.inserted_records, 2)
        self.assertEqual(second.inserted_records, 0)
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT transaction_id, source, energy_kwh FROM ev_sessions ORDER BY transaction_id"
            ).fetchall()
        self.assertEqual(rows, [("csv-001", "ocular_csv", 8.5), ("csv-002", "ocular_csv", 4.25)])

    def test_malformed_csv_row_is_rejected(self):
        self.path.write_text(
            "Transaction ID,Charging start time,Charging stop time,Charging amount (kWh)\n"
            "bad,not-a-time,2026-01-02 01:00,4\n",
            encoding="utf-8",
        )
        records = self.importer.parse(self.path)
        preview = self.importer.preview(records)

        self.assertTrue(any(issue.severity == "error" for issue in preview.issues))
        self.assertIn("Invalid Ocular row", preview.issues[0].message)


if __name__ == "__main__":
    unittest.main()
