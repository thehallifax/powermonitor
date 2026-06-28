import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_ROOT = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import report
import web
from engine.models import (
    Decision,
    DecisionExplanation,
    EnergyScore,
    FinancialImpact,
    TimelineEntry,
)


@dataclass
class TrendSettings:
    supply_charge_per_day: float = 1.0
    import_rates = {
        "super_off_peak": 0.08,
        "overnight": 0.19,
        "off_peak": 0.23,
        "peak": 0.54,
    }
    export_credit_rate: float = 0.02
    ev_baseline_watts: float = 250.0
    ev_data_source: str = "ocular_export"


class PlatformStabilisationTests(unittest.TestCase):
    def test_api_json_dumps_serializes_nested_engine_models(self):
        decision = Decision(
            id="peak_cost_reduction",
            title="Reduce Peak imports",
            summary="Peak import is high.",
            finding="Peak import is high.",
            evidence={"peak_cost": 12.5},
            financial_impact=FinancialImpact(billing_period=4.2),
            recommendation="Shift load.",
            confidence="Medium",
            priority="Medium",
            expires_at="2026-06-30",
            data_sources_used=["SolarEdge interval data"],
            category="Tariffs",
            action_type="load_shift",
            explanation=DecisionExplanation(
                why_this_matters="Peak imports cost more.",
                evidence_used=["Peak cost: 12.50"],
                financial_impact="$4.20 this billing period",
                confidence_reason="Medium confidence from interval data.",
                data_sources_used=["SolarEdge interval data"],
            ),
        )
        score = EnergyScore(
            score=82,
            label="Good",
            summary="Good energy performance.",
            top_positive_factors=["Solar self-consumption"],
            top_negative_factors=["Peak exposure"],
            potential_score=90,
            confidence="High",
            data_sources_used=["SolarEdge interval data"],
            explanation=decision.explanation,
        )
        timeline_entry = TimelineEntry(
            timestamp="2026-06-27",
            title="High peak usage",
            summary="Peak usage was high.",
            financial_impact="$4.20 this billing period",
            confidence="Medium",
            evidence_sources=["SolarEdge interval data"],
            event_type="high_peak_usage",
        )

        encoded = web.api_json_dumps(
            {
                "decision": decision,
                "energy_score": score,
                "timeline": {"entries": [timeline_entry]},
                "ev_session_analytics": {"recommendations": [decision]},
            }
        )
        payload = json.loads(encoded)

        self.assertEqual(payload["decision"]["severity"], "amber")
        self.assertEqual(payload["energy_score"]["explanation"]["why_this_matters"], "Peak imports cost more.")
        self.assertEqual(payload["timeline"]["entries"][0]["event_type"], "high_peak_usage")
        self.assertEqual(payload["ev_session_analytics"]["recommendations"][0]["action"], "Shift load.")

    def test_api_summary_payload_is_valid_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "power.sqlite"
            report_path = Path(temp_dir) / "index.html"
            collect_log_path = Path(temp_dir) / "collect.log"
            original_paths = (report.DB_PATH, report.REPORT_PATH, report.COLLECT_LOG_PATH)
            try:
                report.DB_PATH = db_path
                report.REPORT_PATH = report_path
                report.COLLECT_LOG_PATH = collect_log_path
                with sqlite3.connect(db_path) as conn:
                    create_readings(conn)
                    insert_reading(conn, "2026-06-27T12:00:00", "super_off_peak", 1.4, 0.8, 0.6, 90.0, 0.03)

                payload = report.dashboard_data(
                    today=date(2026, 6, 27),
                    period_kind="custom",
                    start="2026-06-27",
                    end="2026-06-27",
                )
                decoded = json.loads(web.api_json_dumps(payload))
            finally:
                report.DB_PATH, report.REPORT_PATH, report.COLLECT_LOG_PATH = original_paths

        self.assertEqual(decoded["period"]["start"], "2026-06-27")
        self.assertEqual(decoded["household"]["household_id"], "default_household")
        self.assertEqual(decoded["account"]["account_id"], "local_account")
        self.assertEqual(
            decoded["household_membership"]["household_id"],
            "default_household",
        )
        self.assertIn("summary", decoded)
        self.assertIn("energy_score", decoded)

    def test_daily_ev_trend_uses_persisted_sessions_grouped_by_start_date(self):
        conn = sqlite3.connect(":memory:")
        try:
            create_readings(conn)
            insert_reading(conn, "2026-06-16T12:00:00", "super_off_peak", 0.0, 0.0, 0.0, 80.0, 0.02)
            engine = report.data_engine(conn)
            engine.validate_database()
            conn.executemany(
                """
                INSERT INTO ev_sessions (
                    start_time,
                    end_time,
                    last_seen,
                    energy_kwh,
                    source,
                    cost_estimate
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "2026-06-15T22:30:00",
                        "2026-06-16T01:00:00",
                        "2026-06-16T01:00:00",
                        28.37,
                        "ocular_export",
                        5.39,
                    ),
                    (
                        "2026-06-16T20:00:00",
                        "2026-06-16T22:00:00",
                        "2026-06-16T22:00:00",
                        32.93,
                        "ocular_export",
                        6.26,
                    ),
                ],
            )

            trends = report.load_daily_trends(
                conn,
                date(2026, 6, 16),
                date(2026, 6, 16),
                TrendSettings(),
            )
        finally:
            conn.close()

        self.assertEqual(len(trends), 1)
        self.assertEqual(trends[0].ev_charging_source, "ocular_export")
        self.assertAlmostEqual(trends[0].ev_charging_kwh, 32.93)


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


def insert_reading(
    conn,
    measurement_time,
    tariff_bucket,
    production_kwh,
    solar_to_home_kwh,
    solar_to_grid_kwh,
    grid_to_home_w,
    grid_to_home_kwh,
):
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
        (
            measurement_time,
            tariff_bucket,
            production_kwh,
            solar_to_home_kwh,
            solar_to_grid_kwh,
            grid_to_home_w,
            grid_to_home_kwh,
            0.0,
        ),
    )


if __name__ == "__main__":
    unittest.main()
