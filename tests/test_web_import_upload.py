import re
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report
import web
from engine.data_engine import DataEngine


class WebImportUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "power.sqlite"
        self.report_path = Path(self.temp_dir.name) / "index.html"
        self.collect_log_path = Path(self.temp_dir.name) / "collect.log"
        self.upload_dir = Path(self.temp_dir.name) / "uploads"
        self.original_report_paths = (report.DB_PATH, report.REPORT_PATH, report.COLLECT_LOG_PATH)
        self.original_upload_dir = web.UPLOAD_DIR
        report.DB_PATH = self.db_path
        report.REPORT_PATH = self.report_path
        report.COLLECT_LOG_PATH = self.collect_log_path
        web.UPLOAD_DIR = self.upload_dir
        with sqlite3.connect(self.db_path) as conn:
            create_readings(conn)
            insert_reading(conn)
            DataEngine(connection=conn).validate_database()

    def tearDown(self):
        report.DB_PATH, report.REPORT_PATH, report.COLLECT_LOG_PATH = self.original_report_paths
        web.UPLOAD_DIR = self.original_upload_dir
        self.temp_dir.cleanup()

    def test_web_upload_previews_and_commits_ocular_export(self):
        export_path = Path(self.temp_dir.name) / "ocular.xlsx"
        create_ocular_xlsx(export_path)
        server, thread, base_url = start_server()
        try:
            preview = multipart_post(base_url, "/imports/preview", "ocular.xlsx", export_path.read_bytes())
            self.assertIn("Import preview", preview)
            self.assertIn("Commit import", preview)
            job_id = re.search(r'name="job_id" value="([^"]+)"', preview).group(1)

            result = form_post(base_url, "/imports/commit", {"job_id": job_id})
            self.assertIn("Import complete", result)

            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT household_id, COUNT(*), ROUND(SUM(energy_kwh), 2)
                    FROM ev_sessions
                    GROUP BY household_id
                    """
                ).fetchall()
                history = DataEngine(connection=conn).get_import_history()
            self.assertEqual(rows, [("default_household", 2, 10.0)])
            self.assertEqual(history[0]["status"], "completed")
        finally:
            stop_server(server, thread)

    def test_web_upload_rejects_unknown_format(self):
        server, thread, base_url = start_server()
        try:
            with self.assertRaises(HTTPError) as raised:
                multipart_post(base_url, "/imports/preview", "notes.txt", b"hello")
            body = raised.exception.read().decode("utf-8")
            self.assertIn("Unsupported import format", body)
            self.assertNotIn(str(self.temp_dir.name), body)
        finally:
            stop_server(server, thread)


def start_server():
    server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=10)


def multipart_post(base_url: str, path: str, filename: str, content: bytes) -> str:
    boundary = f"----PowerMonitor{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="import_file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return urlopen(request, timeout=10).read().decode("utf-8")


def form_post(base_url: str, path: str, params: dict[str, str]) -> str:
    body = "&".join(f"{key}={value}" for key, value in params.items()).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return urlopen(request, timeout=10).read().decode("utf-8")


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


def insert_reading(conn):
    conn.execute(
        """
        INSERT INTO readings (
            measurement_time,
            tariff_bucket,
            production_kwh,
            solar_to_home_kwh,
            solar_to_grid_kwh,
            grid_to_home_w,
            grid_to_home_kwh,
            import_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("2026-06-27T12:00:00", "peak", 1.0, 0.4, 0.6, 400.0, 2.0, 1.0),
    )


def create_ocular_xlsx(path: Path) -> None:
    rows = [
        [
            "Record number",
            "Charger number",
            "Charging amount",
            "Charging start time",
            "Charging stop time",
            "Charging duration",
            "Starting user",
            "Stopping user",
        ],
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
