import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Optional

from collector.import_ocular_export import OcularEVImporter
from engine.data_engine import DataEngine
from engine.import_framework import (
    ImportPreview,
    ImportSource,
    detect_importer,
)
from engine.models import (
    Account,
    Household,
    HouseholdContext,
    HouseholdMembership,
)


class ImportFrameworkTests(unittest.TestCase):
    def test_detect_importer_returns_none_for_unknown_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "notes.txt"
            path.write_text("not an import", encoding="utf-8")

            self.assertIsNone(detect_importer(path, [OcularEVImporter()]))

    def test_ocular_importer_previews_and_commits_for_household(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            export_path = Path(temp_dir) / "ocular.xlsx"
            db_path = Path(temp_dir) / "power.sqlite"
            create_ocular_xlsx(export_path)
            importer = OcularEVImporter(db_path)

            self.assertTrue(importer.detect(export_path))
            parsed = importer.parse(export_path)
            preview = importer.preview(parsed)
            result = importer.commit(parsed, household_context("household_a"), job_id="job_1")

            self.assertEqual(preview.total_records, 2)
            self.assertEqual(preview.valid_records, 2)
            self.assertAlmostEqual(preview.total_energy_kwh, 10.0)
            self.assertTrue(result.success)
            self.assertEqual(result.inserted_records, 2)

            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT household_id, COUNT(*), ROUND(SUM(energy_kwh), 2) FROM ev_sessions GROUP BY household_id"
                ).fetchall()
                observations = DataEngine(connection=conn, household_context=household_context("household_a")).get_provider_observations()
            self.assertEqual(rows, [("household_a", 2, 10.0)])
            self.assertEqual(len(observations), 2)
            self.assertEqual(observations[0]["provider_id"], "ocular_export")
            self.assertEqual(observations[0]["import_job_id"], "job_1")

    def test_ocular_importer_preserves_connection_values_and_infers_active_interval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            export_path = Path(temp_dir) / "ocular-example.xlsx"
            db_path = Path(temp_dir) / "power.sqlite"
            create_ocular_xlsx(
                export_path,
                rows=[
                    ["oc-example", "charger-1", "30.53", "2026-07-13 16:31:41", "2026-07-14 08:56:07", "16:24:26", "driver", "driver"],
                ],
            )
            importer = OcularEVImporter(db_path)
            parsed = importer.parse(export_path)

            first = importer.commit(parsed, household_context("household_a"), job_id="job_1")
            second = importer.commit(parsed, household_context("household_a"), job_id="job_2")

            self.assertTrue(first.success)
            self.assertTrue(second.success)
            self.assertEqual(first.inserted_records, 1)
            self.assertEqual(second.inserted_records, 0)
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT start_time, end_time, energy_kwh, overnight_kwh, cost_estimate, notes_json
                    FROM ev_sessions
                    WHERE transaction_id = 'oc-example'
                    """
                ).fetchone()
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM ev_sessions WHERE transaction_id = 'oc-example'").fetchone()[0],
                    1,
                )
            notes = json.loads(row[5])
            self.assertEqual(row[0], "2026-07-13T16:31:41+08:00")
            self.assertEqual(row[1], "2026-07-14T08:56:07+08:00")
            self.assertAlmostEqual(row[2], 30.53)
            self.assertAlmostEqual(row[3], 30.53)
            self.assertEqual(notes["estimated_active_start"], "2026-07-13T23:00:00+08:00")
            self.assertTrue(notes["estimated_active_finish"].startswith("2026-07-14T03:25:"))
            self.assertTrue(notes["active_interval_inferred"])

    def test_data_engine_persists_import_history_by_household(self):
        with sqlite3.connect(":memory:") as conn:
            create_readings(conn)
            engine_a = DataEngine(connection=conn, household_context=household_context("household_a"))
            engine_b = DataEngine(connection=conn, household_context=household_context("household_b"))
            engine_a.validate_database()
            source = ImportSource("test_source", "Test Source", (".csv",))
            preview = ImportPreview(source=source, total_records=1, valid_records=1)

            engine_a.create_import_job(source, "a.csv", Path("/tmp/a.csv"), preview=preview)

            self.assertEqual(len(engine_a.get_import_history()), 1)
            self.assertIn("provider_observation_count", engine_a.get_import_history()[0])
            self.assertEqual(engine_b.get_import_history(), [])


def household_context(household_id: str) -> HouseholdContext:
    return HouseholdContext(
        account=Account(f"acct_{household_id}", f"Account {household_id}", "local"),
        household=Household(household_id, f"Home {household_id}", "Australia/Perth"),
        membership=HouseholdMembership(f"acct_{household_id}", household_id, "owner"),
    )


def create_readings(conn: sqlite3.Connection) -> None:
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


def create_ocular_xlsx(path: Path, rows: Optional[list[list[str]]] = None) -> None:
    headers = [
        "Record number",
        "Charger number",
        "Charging amount",
        "Charging start time",
        "Charging stop time",
        "Charging duration",
        "Starting user",
        "Stopping user",
    ]
    rows = [headers, *(rows or [
        ["oc-1", "charger-1", "7.50", "2026-06-16 10:00:00", "2026-06-16 11:00:00", "60", "driver", "driver"],
        ["oc-2", "charger-1", "2.50", "2026-06-17 23:00:00", "2026-06-18 00:00:00", "60", "driver", "driver"],
    ])]
    with zipfile.ZipFile(path, "w") as workbook:
        workbook.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        )
        workbook.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        )
        workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml(rows))


def sheet_xml(rows: list[list[str]]) -> str:
    xml_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row):
            ref = f"{chr(ord('A') + column_index)}{row_index}"
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
        xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>{"".join(xml_rows)}</sheetData>
</worksheet>"""


if __name__ == "__main__":
    unittest.main()
