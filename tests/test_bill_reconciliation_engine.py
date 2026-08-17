import sqlite3
from contextlib import closing
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from engine.bill_reconciliation_engine import BillReconciliation, BillReconciliationEngine
from engine.data_engine import DataEngine


@dataclass
class Summary:
    start: str = "2026-05-01"
    end: str = "2026-05-10"
    supply_days: int = 10
    reading_days: int = 10
    import_cost: float = 96.0
    export_credit: float = 9.0
    supply_cost: float = 38.0
    net_cost: float = 130.0
    ev_charging_source: str = "ocular_export"


@dataclass
class Settings:
    import_rates: dict = field(default_factory=lambda: {
        "peak": 0.50,
        "off_peak": 0.22,
        "overnight": 0.18,
        "super_off_peak": 0.08,
        "anytime": 0.30,
    })
    export_credit_rate: float = 0.025


def bill(**overrides):
    data = {
        "bill_id": "bill-1",
        "retailer": "Synergy",
        "invoice_number": "INV-1",
        "bill_total": 135.0,
        "total_amount_owing": 135.0,
        "opening_balance": 0.0,
        "gst": 5.0,
        "confidence": "high",
        "periods": [{"plan_name": "EV Add On", "start_date": "2026-05-01", "end_date": "2026-05-10"}],
        "line_items": [
            {"category": "import_usage", "bucket": "peak", "description": "Peak Charges", "kwh": 200.0, "unit_rate": 0.50, "amount": 100.0},
            {"category": "export_credit", "bucket": "off_peak_export", "description": "Buyback", "kwh": 400.0, "unit_rate": 0.025, "amount": -10.0},
            {"category": "supply_charge", "bucket": None, "description": "Supply charge", "days": 10, "unit_rate": 4.0, "amount": 40.0},
            {"category": "gst", "bucket": None, "description": "GST", "amount": 5.0},
        ],
        "totals": {},
    }
    data.update(overrides)
    return data


class BillReconciliationEngineTests(unittest.TestCase):
    def test_actual_vs_estimate_variance_and_breakdown(self):
        reconciliation = BillReconciliationEngine().reconcile(bill(), Summary(), Settings())

        self.assertAlmostEqual(reconciliation.actual_bill_total, 135.0)
        self.assertAlmostEqual(reconciliation.estimated_bill_total, 130.0)
        self.assertAlmostEqual(reconciliation.dollar_variance, 5.0)
        self.assertEqual(reconciliation.variance_category, "close")
        by_category = {component.category: component for component in reconciliation.components}
        self.assertAlmostEqual(by_category["import_usage"].variance, 4.0)
        self.assertAlmostEqual(by_category["export_credit"].variance, -1.0)
        self.assertAlmostEqual(by_category["supply_charge"].variance, 2.0)
        self.assertAlmostEqual(by_category["gst"].variance, 5.0)
        self.assertAlmostEqual(by_category["unexplained"].variance, -5.0)
        self.assertIn("real Synergy bill was $5.00 higher", reconciliation.explanation)

    def test_variance_category_thresholds(self):
        engine = BillReconciliationEngine()

        self.assertEqual(engine.reconcile(bill(bill_total=100.5), Summary(net_cost=100.0), Settings()).variance_category, "excellent")
        self.assertEqual(engine.reconcile(bill(bill_total=108.0), Summary(net_cost=100.0), Settings()).variance_category, "close")
        self.assertEqual(engine.reconcile(bill(bill_total=120.0), Summary(net_cost=100.0), Settings()).variance_category, "needs review")
        self.assertEqual(engine.reconcile(bill(bill_total=150.0), Summary(net_cost=100.0), Settings()).variance_category, "unreliable")

    def test_confidence_drops_for_missing_data_low_parser_confidence_and_estimated_ev(self):
        reconciliation = BillReconciliationEngine().reconcile(
            bill(confidence="low", periods=[
                {"plan_name": "Home", "start_date": "2026-05-01", "end_date": "2026-05-05"},
                {"plan_name": "EV Add On", "start_date": "2026-05-06", "end_date": "2026-05-10"},
            ]),
            Summary(reading_days=4, ev_charging_source="estimated"),
            Settings(),
        )

        self.assertEqual(reconciliation.confidence, "low")
        self.assertEqual(reconciliation.variance_category, "unreliable")
        self.assertIn("bill parser had low confidence", " ".join(reconciliation.data_gaps))
        self.assertIn("EV charging", " ".join(reconciliation.data_gaps))

    def test_tariff_mismatch_is_reported(self):
        reconciliation = BillReconciliationEngine().reconcile(
            bill(line_items=[
                {"category": "import_usage", "bucket": "peak", "description": "Peak Charges", "kwh": 200.0, "unit_rate": 0.60, "amount": 120.0},
            ]),
            Summary(),
            Settings(),
        )

        self.assertEqual(reconciliation.confidence, "medium")
        self.assertIn("Peak: bill $0.600000/kWh, configured $0.500000/kWh", reconciliation.tariff_mismatches)
        self.assertIn("tariff rates", " ".join(reconciliation.data_gaps))

    def test_persistence_updates_existing_reconciliation_and_redacts_bill_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "power.sqlite"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                create_readings(conn)
                engine = DataEngine(connection=conn)
                engine.validate_database()
                imported = bill(
                    account_number="123456789",
                    nmi="800123456789",
                    supply_address="44 Example Road",
                    meter_number="MTR123",
                )
                inserted = engine.import_synergy_bill(imported)
                first = BillReconciliation(
                    bill_id=inserted["bill_id"],
                    invoice_number="INV-1",
                    retailer="Synergy",
                    period_start="2026-05-01",
                    period_end="2026-05-10",
                    actual_bill_total=135.0,
                    estimated_bill_total=130.0,
                    dollar_variance=5.0,
                    percentage_variance=3.7,
                    variance_category="close",
                    confidence="high",
                    explanation="First estimate.",
                )
                second = BillReconciliation(
                    bill_id=inserted["bill_id"],
                    invoice_number="INV-1",
                    retailer="Synergy",
                    period_start="2026-05-01",
                    period_end="2026-05-10",
                    actual_bill_total=135.0,
                    estimated_bill_total=132.0,
                    dollar_variance=3.0,
                    percentage_variance=2.2,
                    variance_category="close",
                    confidence="high",
                    explanation="Updated estimate.",
                )
                engine.persist_bill_reconciliation(first)
                engine.persist_bill_reconciliation(second)
                latest = engine.get_latest_bill_reconciliation()
                rows = conn.execute("SELECT COUNT(*) FROM bill_reconciliations").fetchone()[0]

        self.assertEqual(rows, 1)
        self.assertEqual(latest["estimated_bill_total"], 132.0)
        self.assertEqual(latest["difference"], 3.0)
        self.assertEqual(latest["latest_bill"]["account_number"], "•••789")
        self.assertEqual(latest["latest_bill"]["nmi"], "•••789")
        self.assertNotIn("supply_address", latest["latest_bill"])


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
