import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import collector.import_solaredge as import_solaredge


class SolarEdgeImportTests(unittest.TestCase):
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
            original_db_path = import_solaredge.DB_PATH
            original_argv = sys.argv
            import_solaredge.DB_PATH = db_path
            try:
                sys.argv = ["import_solaredge.py", str(json_path)]
                first_output = StringIO()
                with redirect_stdout(first_output):
                    import_solaredge.main()
                second_output = StringIO()
                with redirect_stdout(second_output):
                    import_solaredge.main()
            finally:
                import_solaredge.DB_PATH = original_db_path
                sys.argv = original_argv

            with sqlite3.connect(db_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

        self.assertEqual(count, 2)
        self.assertIn("data received 2", first_output.getvalue())
        self.assertIn("time range imported 2026-06-30T09:00:00+08:00 to 2026-06-30T09:15:00+08:00", first_output.getvalue())
        self.assertIn("records imported 2", first_output.getvalue())
        self.assertIn("duplicates ignored 2", second_output.getvalue())
        self.assertIn("observations promoted 0", first_output.getvalue())


if __name__ == "__main__":
    unittest.main()
