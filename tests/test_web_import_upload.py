import re
import sqlite3
from contextlib import closing
import sys
import tempfile
import threading
import unittest
import uuid
import zipfile
import json
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report
import web
import import_synergy_bill
from engine.data_engine import DataEngine
from engine.tariff_importing import TariffUpdateRepository

SYNERGY_BILL_TEXT = """
Synergy Tax Invoice
Account number: 123 456 789
Invoice number: INV-2026-0701
Date of issue: 04 Jul 2026
Direct debit date: 18 Jul 2026
Supply address: 44 Example Road, Thornlie WA 6108
NMI: 8001 2345 678
Home Plan 06 Jun 2026 to 30 Jun 2026
Anytime usage 210.50 kWh @ $0.310000 $65.26
Supply charge 25 days @ $1.292300 $32.31
EV Add On 01 Jul 2026 to 03 Jul 2026
Peak usage 12.00 kWh @ $0.538446 $6.46
Super Off Peak usage 34.00 kWh @ $0.086151 $2.93
Off peak export 9.00 kWh @ $0.025250 -$0.23
GST $10.15
Total bill amount $125.80
Total amount owing $125.80
"""


class WebImportUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "power.sqlite"
        self.report_path = Path(self.temp_dir.name) / "index.html"
        self.collect_log_path = Path(self.temp_dir.name) / "collect.log"
        self.settings_path = Path(self.temp_dir.name) / "settings.json"
        self.upload_dir = Path(self.temp_dir.name) / "uploads"
        self.original_report_paths = (
            report.DB_PATH,
            report.REPORT_PATH,
            report.COLLECT_LOG_PATH,
            report.SETTINGS_PATH,
        )
        self.original_upload_dir = web.UPLOAD_DIR
        report.DB_PATH = self.db_path
        report.REPORT_PATH = self.report_path
        report.COLLECT_LOG_PATH = self.collect_log_path
        report.SETTINGS_PATH = self.settings_path
        web.UPLOAD_DIR = self.upload_dir
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            create_readings(conn)
            insert_reading(conn)
            DataEngine(connection=conn).validate_database()

    def tearDown(self):
        report.DB_PATH, report.REPORT_PATH, report.COLLECT_LOG_PATH, report.SETTINGS_PATH = self.original_report_paths
        web.UPLOAD_DIR = self.original_upload_dir
        self.temp_dir.cleanup()

    def write_settings(self, **overrides):
        settings = {
            "billing_period_start_day": 6,
            "current_billing_period_start": "2026-06-06",
            "current_billing_period_end": "2026-08-06",
            "previous_billing_period_start": "2026-04-03",
            "previous_billing_period_end": "2026-06-05",
        }
        settings.update(overrides)
        self.settings_path.write_text(json.dumps(settings), encoding="utf-8")

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

            with closing(sqlite3.connect(self.db_path)) as conn, conn:
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
            self.assertIn("That file type is not supported yet", body)
            self.assertIn("Ocular Excel export", body)
            self.assertNotIn(str(self.temp_dir.name), body)
        finally:
            stop_server(server, thread)

    def test_web_upload_previews_and_commits_synergy_pdf_bill(self):
        server, thread, base_url = start_server()
        try:
            with patch.object(import_synergy_bill, "extract_text_from_pdf", return_value=SYNERGY_BILL_TEXT):
                preview = multipart_post(base_url, "/imports/preview", "synergy.pdf", b"%PDF placeholder")
                self.assertIn("Import preview", preview)
                self.assertIn("Synergy PDF bill", preview)
                self.assertIn("INV-2026-0701", preview)
                self.assertIn("•••789", preview)
                self.assertNotIn("123 456 789", preview)
                self.assertNotIn("44 Example Road", preview)
                job_id = re.search(r'name="job_id" value="([^"]+)"', preview).group(1)

                result = form_post(base_url, "/imports/commit", {"job_id": job_id})
                self.assertIn("Import complete", result)

            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                latest = DataEngine(connection=conn).get_latest_synergy_bill()
                history = DataEngine(connection=conn).get_import_history()

            self.assertEqual(latest["invoice_number"], "INV-2026-0701")
            self.assertEqual(latest["account_number"], "•••789")
            self.assertEqual(history[0]["source"]["source_id"], "synergy_bill_pdf")
        finally:
            stop_server(server, thread)

    def test_dashboard_does_not_auto_refresh_during_upload_workflow(self):
        server, thread, base_url = start_server()
        try:
            response = urlopen(f"{base_url}/", timeout=10)
            body = response.read().decode("utf-8")

            self.assertIsNone(response.headers.get("Refresh"))
            self.assertIn("will not auto-refresh", body)
        finally:
            stop_server(server, thread)

    def test_glance_route_keeps_kiosk_refresh(self):
        server, thread, base_url = start_server()
        try:
            response = urlopen(f"{base_url}/glance", timeout=10)
            body = response.read().decode("utf-8")

            self.assertEqual(response.headers.get("Refresh"), "300")
            self.assertIn("glance-display", body)
        finally:
            stop_server(server, thread)

    def test_generated_report_contains_tariff_update_section(self):
        report.generate_report(self.report_path)

        html = self.report_path.read_text(encoding="utf-8")

        self.assertIn('id="tariff-updates"', html)
        self.assertIn("Current active tariff", html)
        self.assertIn("Check for tariff updates", html)

    def test_live_root_serves_generated_tariff_update_section(self):
        report.generate_report(self.report_path)
        server, thread, base_url = start_server()
        try:
            html = urlopen(f"{base_url}/", timeout=10).read().decode("utf-8")

            self.assertIn('id="tariff-updates"', html)
            self.assertIn("Current active tariff", html)
            self.assertIn("Check for tariff updates", html)
        finally:
            stop_server(server, thread)

    def test_live_root_serves_regenerated_html_on_next_request(self):
        self.report_path.write_text("<html><body><section id='tariff-updates'>first version</section></body></html>", encoding="utf-8")
        server, thread, base_url = start_server()
        try:
            first = urlopen(f"{base_url}/", timeout=10).read().decode("utf-8")
            self.assertIn("first version", first)

            self.report_path.write_text("<html><body><section id='tariff-updates'>second version</section></body></html>", encoding="utf-8")

            second = urlopen(f"{base_url}/", timeout=10).read().decode("utf-8")
            self.assertIn("second version", second)
            self.assertNotIn("first version", second)
        finally:
            stop_server(server, thread)

    def test_default_root_matches_canonical_generated_report(self):
        self.write_settings()
        expected = report.generate_report(self.report_path)
        server, thread, base_url = start_server()
        try:
            html = urlopen(f"{base_url}/", timeout=10).read().decode("utf-8")
        finally:
            stop_server(server, thread)

        self.assertEqual(html, expected)

    def test_last_7_days_query_renders_requested_period_without_overwriting_canonical_report(self):
        self.write_settings()
        canonical = report.generate_report(self.report_path)
        server, thread, base_url = start_server()
        try:
            html = urlopen(f"{base_url}/?period=last_7_days", timeout=10).read().decode("utf-8")
        finally:
            stop_server(server, thread)

        self.assertIn("Showing <strong>Last 7 days</strong>", html)
        self.assertIn('option value="last_7_days" selected', html)
        today = report.local_today()
        self.assertIn(f'name="start" value="{(today - timedelta(days=6)).isoformat()}"', html)
        self.assertIn(f'name="end" value="{today.isoformat()}"', html)
        self.assertNotEqual(html, canonical)
        self.assertEqual(self.report_path.read_text(encoding="utf-8"), canonical)

    def test_last_30_days_query_renders_requested_period(self):
        self.write_settings()
        report.generate_report(self.report_path)
        server, thread, base_url = start_server()
        try:
            html = urlopen(f"{base_url}/?period=last_30_days", timeout=10).read().decode("utf-8")
        finally:
            stop_server(server, thread)

        self.assertIn("Showing <strong>Last 30 days</strong>", html)
        self.assertIn('option value="last_30_days" selected', html)
        today = report.local_today()
        self.assertIn(f'name="start" value="{(today - timedelta(days=29)).isoformat()}"', html)
        self.assertIn(f'name="end" value="{today.isoformat()}"', html)

    def test_previous_billing_period_query_renders_requested_period(self):
        self.write_settings()
        report.generate_report(self.report_path)
        server, thread, base_url = start_server()
        try:
            html = urlopen(f"{base_url}/?period=previous_billing_period", timeout=10).read().decode("utf-8")
        finally:
            stop_server(server, thread)

        self.assertIn("Showing <strong>Previous billing period</strong>", html)
        self.assertIn('name="start" value="2026-06-06"', html)
        self.assertIn('name="end" value="2026-08-06"', html)

    def test_summary_api_exposes_rolled_billing_period_metadata(self):
        self.write_settings(
            current_billing_period_end=None,
            previous_billing_period_start="2026-04-03",
            previous_billing_period_end="2026-06-05",
        )
        server, thread, base_url = start_server()
        try:
            with patch.object(report, "local_today", return_value=date(2026, 8, 17)):
                payload = json.loads(urlopen(f"{base_url}/api/summary", timeout=10).read().decode("utf-8"))
        finally:
            stop_server(server, thread)

        self.assertEqual(payload["period"]["start"], "2026-08-09")
        self.assertEqual(payload["period"]["end"], "2026-08-17")
        self.assertEqual(payload["billing_period_resolution"]["expected_billing_period_end"], "2026-10-11")
        self.assertTrue(payload["billing_period_resolution"]["current_boundary_predicted"])

    def test_production_report_path_uses_canonical_rolled_period(self):
        self.write_settings(
            current_billing_period_end=None,
            previous_billing_period_start="2026-04-03",
            previous_billing_period_end="2026-06-05",
        )
        with patch.object(report, "local_today", return_value=date(2026, 8, 17)):
            markup = report.generate_report(self.report_path)

        self.assertIn('name="start" value="2026-08-09"', markup)
        self.assertIn('name="end" value="2026-08-17"', markup)
        self.assertIn("2026-06-06 to 2026-08-08", markup)
        self.assertIn("expected_billing_period_end</span><strong>2026-10-11", markup)

    def test_custom_period_query_renders_requested_dates(self):
        self.write_settings()
        report.generate_report(self.report_path)
        server, thread, base_url = start_server()
        try:
            html = urlopen(f"{base_url}/?period=custom&start=2026-06-01&end=2026-06-15", timeout=10).read().decode("utf-8")
        finally:
            stop_server(server, thread)

        self.assertIn("Showing <strong>Custom period</strong>", html)
        self.assertIn('name="start" value="2026-06-01"', html)
        self.assertIn('name="end" value="2026-06-15"', html)

    def test_invalid_period_query_falls_back_with_visible_message(self):
        self.write_settings()
        report.generate_report(self.report_path)
        server, thread, base_url = start_server()
        try:
            html = urlopen(f"{base_url}/?period=made_up_period", timeout=10).read().decode("utf-8")
        finally:
            stop_server(server, thread)

        self.assertIn("That dashboard period was not recognised.", html)
        self.assertIn("Showing Current billing period instead.", html)
        self.assertIn("Showing <strong>Current billing period</strong>", html)

    def test_invalid_custom_dates_fall_back_with_visible_message(self):
        self.write_settings()
        report.generate_report(self.report_path)
        server, thread, base_url = start_server()
        try:
            html = urlopen(f"{base_url}/?period=custom&start=2026-06-16&end=2026-06-01", timeout=10).read().decode("utf-8")
        finally:
            stop_server(server, thread)

        self.assertIn("Custom dashboard period start must be on or before the end date.", html)
        self.assertIn("Showing Current billing period instead.", html)

    def test_tariff_update_post_routes_still_work(self):
        report.generate_report(self.report_path)
        repository = TariffUpdateRepository(report.tariff_updates_path())
        server, thread, base_url = start_server()
        try:
            def fake_check(_settings, _summary=None):
                repository.stage_pending(sample_tariff_update("pending-check"))
                return {"status": "update_staged"}

            with patch.object(report, "check_tariff_updates", side_effect=fake_check):
                check_result = form_post(base_url, "/tariff-updates/check", {})
            self.assertIn("Tariff updates", check_result)
            self.assertEqual(len(repository.pending_updates()), 1)

            approve_id = repository.pending_updates()[0]["update_id"]
            approve_result = form_post(base_url, "/tariff-updates/approve", {"update_id": approve_id})
            self.assertIn("Tariff updates", approve_result)
            self.assertIn("Tariff update approved.", approve_result)
            self.assertIn("Historical periods were not rewritten", approve_result)
            self.assertEqual(repository.pending_updates(), [])

            repository.stage_pending(sample_tariff_update("pending-dismiss"))
            dismiss_id = repository.pending_updates()[0]["update_id"]
            dismiss_result = form_post(base_url, "/tariff-updates/dismiss", {"update_id": dismiss_id})
            self.assertIn("Tariff updates", dismiss_result)
            self.assertEqual(repository.pending_updates(), [])
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
    body = urlencode(params).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return urlopen(request, timeout=10).read().decode("utf-8")


def sample_tariff_update(update_id: str) -> dict:
    return {
        "update_id": update_id,
        "tariff_name": "Synergy EV Add On",
        "date_detected": "2026-07-12",
        "effective_date": "2026-08-01",
        "current_checksum": "current-checksum",
        "pending_checksum": "pending-checksum",
        "current_version": {
            "provider": "Synergy",
            "tariff_name": "Synergy EV Add On",
            "version": "2026.07",
            "effective_from": "2026-07-01",
            "version_hash": "current-checksum",
            "supply_charge_per_day": 1.2923,
            "usage_rates": {
                "super_off_peak": 0.086151,
                "overnight": 0.193841,
                "off_peak": 0.236916,
                "peak": 0.538446,
            },
            "export_rates": {"standard": 0.02525},
        },
        "pending_version": {
            "provider": "Synergy",
            "tariff_name": "Synergy EV Add On",
            "version": "2026.08",
            "effective_from": "2026-08-01",
            "version_hash": "pending-checksum",
            "supply_charge_per_day": 1.3023,
            "usage_rates": {
                "super_off_peak": 0.096151,
                "overnight": 0.203841,
                "off_peak": 0.246916,
                "peak": 0.548446,
            },
            "export_rates": {"standard": 0.02625},
        },
        "diff": [
            {"label": "Peak usage", "before": "$0.538446", "after": "$0.548446"},
        ],
        "financial_preview": {
            "available": True,
            "annual_bill_difference": 42.0,
            "summary": "Higher peak pricing would increase the annual bill.",
            "battery_recommendation_impact": "",
            "plan_recommendation_impact": "",
        },
    }


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
