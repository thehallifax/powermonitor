import sqlite3
import unittest
from dataclasses import dataclass
from datetime import date

from engine.data_engine import DataEngine
from engine.decision_engine import generate_decisions_from_data_engine


@dataclass
class Settings:
    billing_period_start_day: int = 6
    current_billing_period_start: date = date(2026, 6, 6)
    current_billing_period_end = None
    previous_billing_period_start = None
    previous_billing_period_end = None
    import_rates = {
        "super_off_peak": 0.08,
        "overnight": 0.19,
        "off_peak": 0.23,
        "peak": 0.54,
    }
    ev_baseline_watts = 250.0
    recommendation_thresholds = {
        "peak_cost_share_pct": 25.0,
        "peak_shift_kwh_per_day": 5.0,
        "solar_self_consumption_pct": 60.0,
        "ev_detected_min_kwh": 0.5,
        "baseload_excess_pct": 20.0,
        "high_cost_threshold": 300.0,
        "positive_peak_share_pct": 20.0,
        "positive_self_consumption_pct": 70.0,
    }


class DataEngineTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
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
        self.conn.executemany(
            """
            INSERT INTO readings (
                measurement_time, tariff_bucket, production_kwh,
                solar_to_home_kwh, solar_to_grid_kwh, grid_to_home_w,
                grid_to_home_kwh, import_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("2026-06-27T00:15:00", "overnight", 0.0, 0.0, 0.0, 320.0, 0.08, 0.02),
                ("2026-06-27T00:30:00", "overnight", 0.0, 0.0, 0.0, 280.0, 0.07, 0.02),
                ("2026-06-27T12:00:00", "super_off_peak", 1.4, 0.8, 0.6, 90.0, 0.03, 0.01),
            ],
        )
        self.engine = DataEngine(connection=self.conn, health_loader=lambda: {"status": "ok"})
        self.engine.validate_database()
        self.conn.execute(
            """
            INSERT INTO ev_sessions (
                start_time, end_time, energy_kwh, source, cost_estimate,
                transaction_id, super_off_peak_kwh, super_off_peak_cost,
                overnight_kwh, overnight_cost, off_peak_kwh, off_peak_cost,
                peak_kwh, peak_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-27T10:00:00",
                "2026-06-27T11:00:00",
                7.5,
                "ocular_export",
                0.65,
                "abc123",
                7.5,
                0.65,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            ),
        )

    def tearDown(self):
        self.conn.close()

    def test_get_bill_period_uses_configured_start(self):
        period = self.engine.get_bill_period(date(2026, 6, 27), Settings())

        self.assertEqual(period.start, date(2026, 6, 6))
        self.assertEqual(period.end, date(2026, 6, 27))
        self.assertEqual(period.as_dict()["kind"], "current_billing_period")

    def test_get_intervals_and_current_summary_do_not_calculate_money(self):
        grid = self.engine.get_grid_intervals(date(2026, 6, 27), date(2026, 6, 27))
        solar = self.engine.get_solar_intervals(date(2026, 6, 27), date(2026, 6, 27))
        summary = self.engine.get_current_bill_summary(date(2026, 6, 27), date(2026, 6, 27))

        self.assertEqual(len(grid), 3)
        self.assertEqual(len(solar), 3)
        self.assertAlmostEqual(summary["grid_import_kwh"], 0.18)
        self.assertNotIn("net_cost", summary)

    def test_get_ev_sessions_normalises_tariff_breakdown(self):
        sessions = self.engine.get_ev_sessions(
            date(2026, 6, 27),
            date(2026, 6, 27),
            sources=["ocular_export"],
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["transaction_id"], "abc123")
        self.assertEqual(sessions[0]["dominant_tariff"], "super_off_peak")
        self.assertAlmostEqual(sessions[0]["tariff_breakdown"]["super_off_peak"]["kwh"], 7.5)

    def test_completeness_and_snapshot_use_shared_status_models(self):
        completeness = self.engine.get_data_completeness(
            {
                "solar_generated_kwh": 1.4,
                "solar_used_kwh": 0.8,
                "solar_exported_kwh": 0.6,
                "intervals": 3,
                "ev_charging_source": "ocular_export",
            },
            billing_configured=True,
            confidence="High",
            confidence_note="Complete test data",
        )
        snapshot = self.engine.get_latest_household_snapshot(date(2026, 6, 27))

        self.assertEqual(completeness.confidence, "High")
        self.assertEqual(snapshot.freshness.state, "fresh")
        self.assertEqual(snapshot.connector_status.state, "ready")

    def test_source_counts_are_normalised(self):
        counts = self.engine.get_ev_source_counts()

        self.assertEqual(counts[0]["source"], "ocular_export")
        self.assertEqual(counts[0]["sessions"], 1)

    def test_decision_engine_consumes_data_engine_for_baseload(self):
        settings = Settings()
        summary = {
            "start": date(2026, 6, 27),
            "end": date(2026, 6, 27),
            "supply_days": 1,
            "reading_days": 1,
            "peak_cost_share_pct": 0.0,
            "buckets": {"peak": {"kwh": 0.0, "cost": 0.0}},
            "self_consumption_pct": 100.0,
            "solar_export_pct": 0.0,
            "ev_charging_kwh": 0.0,
            "ev_charging_cost": 0.0,
            "ev_charging_source": "estimated",
            "net_cost": 1.0,
            "average_daily_net_cost": 1.0,
        }

        decisions = generate_decisions_from_data_engine(
            summary,
            settings,
            self.engine,
            {**settings.recommendation_thresholds, "baseload_excess_pct": 10.0},
        )

        self.assertIn("baseload_monitoring", [decision.id for decision in decisions])


if __name__ == "__main__":
    unittest.main()
