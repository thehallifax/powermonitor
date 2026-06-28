import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

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
            self.assertEqual(rows, [("household_a", 2, 10.0)])

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


def create_ocular_xlsx(path: Path) -> None:
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
    rows = [
        headers,
        ["oc-1", "charger-1", "7.50", "2026-06-16 10:00:00", "2026-06-16 11:00:00", "60", "driver", "driver"],
        ["oc-2", "charger-1", "2.50", "2026-06-17 23:00:00", "2026-06-18 00:00:00", "60", "driver", "driver"],
    ]
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
