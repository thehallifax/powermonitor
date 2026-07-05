import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys

COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import collector.import_synergy_bill as import_synergy_bill
from collector.import_synergy_bill import SynergyBillImporter, mask_identifier, parse_synergy_bill_text
from engine.data_engine import DataEngine


SYNERGY_BILL_TEXT = """
Synergy Tax Invoice
Account number: 123 456 789
Invoice number: INV-2026-0701
Date of issue: 04 Jul 2026
Direct debit date: 18 Jul 2026
Opening balance: $12.50 CR
Supply address: 44 Example Road, Thornlie WA 6108
NMI: 8001 2345 678
Next scheduled read date: 08 Aug 2026
Meter number: MTR123456

Home Plan 06 Jun 2026 to 30 Jun 2026
Anytime usage 210.50 kWh @ $0.310000 $65.26
Peak export 14.00 kWh @ $0.025250 -$0.35
Supply charge 25 days @ $1.292300 $32.31

EV Add On 01 Jul 2026 to 03 Jul 2026
Peak usage 12.00 kWh @ $0.538446 $6.46
Off peak usage 8.00 kWh @ $0.236916 $1.90
Super Off Peak usage 34.00 kWh @ $0.086151 $2.93
Overnight usage 18.00 kWh @ $0.193841 $3.49
Off peak export 9.00 kWh @ $0.025250 -$0.23
Supply charge 3 days @ $1.292300 $3.88

GST $10.15
Total bill amount $125.80
Total amount owing $125.80
"""


class SynergyBillImportTests(unittest.TestCase):
    def test_parse_synergy_pdf_text_structure_with_multiple_plan_periods(self):
        bill = parse_synergy_bill_text(SYNERGY_BILL_TEXT)

        self.assertEqual(bill.retailer, "Synergy")
        self.assertEqual(bill.account_number, "123 456 789")
        self.assertEqual(bill.invoice_number, "INV-2026-0701")
        self.assertEqual(bill.issue_date, "2026-07-04")
        self.assertEqual(bill.due_date, "2026-07-18")
        self.assertEqual(bill.bill_total, 125.80)
        self.assertEqual(bill.total_amount_owing, 125.80)
        self.assertEqual(bill.opening_balance, -12.50)
        self.assertEqual(bill.nmi, "8001 2345 678")
        self.assertEqual(bill.next_scheduled_read_date, "2026-08-08")
        self.assertEqual(bill.meter_number, "MTR123456")
        self.assertEqual(len(bill.periods), 2)
        self.assertEqual(bill.periods[0]["plan_name"], "Home Plan")
        self.assertEqual(bill.periods[1]["plan_name"], "EV Add On")
        self.assertIn("Multiple plan periods detected", " ".join(bill.warnings))

    def test_ev_add_on_buckets_exports_supply_gst_and_totals_are_extracted(self):
        bill = parse_synergy_bill_text(SYNERGY_BILL_TEXT)
        data = bill.as_dict(redacted=True)
        totals = data["totals"]

        self.assertAlmostEqual(totals["usage_by_bucket"]["peak"]["kwh"], 12.0)
        self.assertAlmostEqual(totals["usage_by_bucket"]["off_peak"]["kwh"], 8.0)
        self.assertAlmostEqual(totals["usage_by_bucket"]["super_off_peak"]["kwh"], 34.0)
        self.assertAlmostEqual(totals["usage_by_bucket"]["overnight"]["kwh"], 18.0)
        self.assertAlmostEqual(totals["usage_by_bucket"]["anytime"]["kwh"], 210.5)
        self.assertAlmostEqual(totals["usage_by_bucket"]["peak_export"]["kwh"], 14.0)
        self.assertAlmostEqual(totals["usage_by_bucket"]["off_peak_export"]["kwh"], 9.0)
        self.assertAlmostEqual(totals["export_credits"], -0.58)
        self.assertAlmostEqual(totals["supply_charge"], 36.19)
        self.assertAlmostEqual(totals["gst"], 10.15)
        self.assertAlmostEqual(totals["bill_total"], 125.80)

    def test_masked_account_and_nmi_display(self):
        bill = parse_synergy_bill_text(SYNERGY_BILL_TEXT)
        redacted = bill.as_dict(redacted=True)

        self.assertEqual(mask_identifier("123456789"), "•••789")
        self.assertEqual(redacted["account_number"], "•••789")
        self.assertEqual(redacted["nmi"], "•••678")
        self.assertIsNone(redacted["supply_address"])

    def test_preview_before_commit_and_duplicate_invoice_handling(self):
        bill = parse_synergy_bill_text(SYNERGY_BILL_TEXT)
        importer = SynergyBillImporter()
        preview = importer.preview(bill)

        self.assertEqual(preview.valid_records, 1)
        self.assertEqual(preview.date_start, "2026-06-06")
        self.assertEqual(preview.date_end, "2026-07-03")
        self.assertEqual(preview.sample_records[0]["account_number"], "•••789")
        self.assertEqual(preview.sample_records[0]["nmi"], "•••678")

        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "power.sqlite"
            with sqlite3.connect(db_path) as conn:
                create_readings(conn)
                DataEngine(connection=conn).validate_database()
                engine = DataEngine(connection=conn)
                inserted = engine.import_synergy_bill(bill.as_dict(), import_job_id="job-1")
                duplicate = engine.import_synergy_bill(bill.as_dict(), import_job_id="job-2")
                latest = engine.get_latest_synergy_bill()
                comparison = engine.compare_projected_to_latest_synergy_bill(120.0)

        self.assertEqual(inserted["status"], "inserted")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(latest["account_number"], "•••789")
        self.assertEqual(latest["nmi"], "•••678")
        self.assertNotIn("supply_address", latest)
        self.assertAlmostEqual(comparison["actual_bill"], 125.80)
        self.assertAlmostEqual(comparison["projected_bill"], 120.0)
        self.assertAlmostEqual(comparison["difference"], -5.80)
        self.assertEqual(comparison["status"], "projection_lower")

    def test_pdf_import_uses_pure_python_when_pdftotext_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bill.pdf"
            path.write_bytes(b"%PDF placeholder")

            with patch.object(import_synergy_bill.shutil, "which", return_value=None), \
                patch.object(import_synergy_bill, "extract_text_with_pypdf", return_value=SYNERGY_BILL_TEXT), \
                patch.object(import_synergy_bill, "extract_text_with_pdftotext") as pdftotext:
                text = import_synergy_bill.extract_text_from_pdf(path)

        self.assertIn("INV-2026-0701", text)
        pdftotext.assert_not_called()

    def test_pdf_import_falls_back_to_pdfminer_when_pypdf_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bill.pdf"
            path.write_bytes(b"%PDF placeholder")

            with patch.object(import_synergy_bill.shutil, "which", return_value=None), \
                patch.object(import_synergy_bill, "extract_text_with_pypdf", side_effect=ImportError("missing pypdf")), \
                patch.object(import_synergy_bill, "extract_text_with_pdfminer", return_value=SYNERGY_BILL_TEXT):
                text = import_synergy_bill.extract_text_from_pdf(path)

        self.assertIn("INV-2026-0701", text)

    def test_pdf_import_reports_clear_failure_when_all_extractors_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bill.pdf"
            path.write_bytes(b"%PDF placeholder")

            with patch.object(import_synergy_bill.shutil, "which", return_value=None), \
                patch.object(import_synergy_bill, "extract_text_with_pypdf", side_effect=ImportError("missing pypdf")), \
                patch.object(import_synergy_bill, "extract_text_with_pdfminer", side_effect=ImportError("missing pdfminer")):
                with self.assertRaises(ValueError) as raised:
                    import_synergy_bill.extract_text_from_pdf(path)

        self.assertIn(
            "PDF text extraction failed; install pypdf/pdfminer or provide a text-readable PDF.",
            str(raised.exception),
        )


def create_readings(conn):
    conn.execute(
        """
        CREATE TABLE readings (
            household_id TEXT DEFAULT 'default_household',
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
