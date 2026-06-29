import sqlite3
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report
from engine.data_engine import DataEngine
from engine.recommendation_engine import generate_recommendations
from engine.models import (
    Advisor,
    AdvisorCollection,
    EvidenceTrail,
    FinancialImpact,
)


@dataclass
class Settings:
    import_rates: dict


class CapabilityDetectionTests(unittest.TestCase):
    def test_data_engine_detects_household_capabilities_from_scoped_data(self):
        with sqlite3.connect(":memory:") as conn:
            create_readings(conn)
            engine = DataEngine(connection=conn)
            engine.validate_database()
            insert_reading(conn, "default_household", production_kwh=1.2, grid_kwh=0.4, import_cost=0.12)
            insert_ev_session(conn, "default_household", "ev-1", 7.5)

            summary = engine.get_household_capability_summary(
                date(2026, 6, 27),
                Settings(import_rates=valid_rates()),
            )

            self.assertTrue(summary.get("solar").usable)
            self.assertTrue(summary.get("ev").usable)
            self.assertTrue(summary.get("grid_smart_meter").usable)
            self.assertTrue(summary.get("tariff").usable)
            self.assertTrue(summary.get("imports").available)
            self.assertFalse(summary.get("battery").usable)

    def test_advisor_collection_filters_missing_ev_solar_and_tariff(self):
        with sqlite3.connect(":memory:") as conn:
            create_readings(conn)
            engine = DataEngine(connection=conn)
            engine.validate_database()
            insert_reading(conn, "default_household", production_kwh=0.0, grid_kwh=0.4, import_cost=0.0)
            summary = engine.get_household_capability_summary(
                date(2026, 6, 27),
                Settings(import_rates={}),
            )

            collection = AdvisorCollection(
                [
                    advisor("charging_advisor", "EV_CHARGING"),
                    advisor("solar_advisor", "SOLAR"),
                    advisor("cost_advisor", "COST"),
                    advisor("efficiency_advisor", "EFFICIENCY"),
                ],
                capability_summary=summary,
            )

            self.assertEqual([item.advisor_type for item in collection.active()], ["EFFICIENCY"])
            inactive = {item["advisor_type"]: item["missing_capabilities"] for item in collection.inactive()}
            self.assertEqual(inactive["EV_CHARGING"], ["ev", "tariff"])
            self.assertEqual(inactive["SOLAR"], ["solar"])
            self.assertEqual(inactive["COST"], ["tariff"])

    def test_dashboard_api_serializes_capability_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "power.sqlite"
            original_db_path = report.DB_PATH
            report.DB_PATH = db_path
            try:
                with sqlite3.connect(db_path) as conn:
                    create_readings(conn)
                    DataEngine(connection=conn).validate_database()
                    insert_reading(conn, "default_household", production_kwh=1.2, grid_kwh=0.4, import_cost=0.12)

                payload = report.dashboard_data(today=date(2026, 6, 27))

                self.assertIn("capability_summary", payload)
                self.assertEqual(payload["capability_summary"]["household_id"], "default_household")
                capability_ids = {
                    item["capability_id"]
                    for item in payload["capability_summary"]["capabilities"]
                }
                self.assertIn("solar", capability_ids)
                self.assertIn("imports", capability_ids)
                self.assertIn("setup_progress", payload)
                self.assertIn("setup_tasks", payload)
                self.assertIn("setup_sections", payload)
                self.assertIn("provider_registry", payload)
                self.assertIn("provider_status", payload)
                self.assertIn("provider_management", payload)
                self.assertIn("providers", payload)
                self.assertIn("provider_runtime", payload)
                self.assertIn("runtime", payload)
                self.assertIn("household_state", payload)
                self.assertIn("provider_observations", payload)
                self.assertIn("promotion_summary", payload)
                self.assertIn("conflict_summary", payload)
                self.assertIn("latest_promotions", payload)
                self.assertIn("context", payload)
                self.assertIn("solaredge", payload["provider_status"])
                self.assertTrue(payload["provider_registry"]["providers"])
                self.assertIn("latest", payload["provider_observations"])
                self.assertTrue(payload["provider_management"]["providers"])
                self.assertIn("solaredge", payload["provider_runtime"]["active_providers"])
                self.assertIn("tariff", payload["household_state"])
                self.assertIn("evidence", payload["household_state"])
                self.assertNotIn("cookie_value", str(payload["provider_management"]))
                self.assertIn("ACCEPTED", payload["promotion_summary"]["counts"])
                self.assertIn("confidence", payload["context"])
                self.assertIn("tariff", payload["context"])
                self.assertIsInstance(payload["setup_progress"]["completion_pct"], int)
                self.assertTrue(
                    all("task_id" in item for item in payload["setup_tasks"])
                )
            finally:
                report.DB_PATH = original_db_path

    def test_missing_tariff_generates_data_quality_recommendation(self):
        recommendations = generate_recommendations(
            {
                "current": {
                    "ev_charging_kwh": 0.0,
                    "peak_cost_share_pct": 0.0,
                    "self_consumption_pct": 100.0,
                    "solar_generated_kwh": 0.0,
                    "average_daily_grid_import_kwh": 0.0,
                },
                "previous": None,
                "ev_data_source": "estimated",
                "import_rates": {},
                "overnight_baseload_watts": 0.0,
                "ev_baseline_watts": 250.0,
                "projected_net_cost": 0.0,
                "projected_period_days": 1,
                "capability_summary": {
                    "capabilities": [
                        {
                            "capability_id": "tariff",
                            "usable": False,
                            "reason": "No configured tariff rates found.",
                        }
                    ]
                },
            },
            {
                "ev_detected_min_kwh": 0.5,
                "high_cost_threshold": 300.0,
                "peak_cost_share_pct": 25.0,
                "peak_shift_kwh_per_day": 5.0,
                "solar_self_consumption_pct": 60.0,
                "baseload_excess_pct": 20.0,
                "positive_peak_share_pct": 20.0,
                "positive_self_consumption_pct": 70.0,
            },
        )

        self.assertIn("data_quality_tariff_setup", [item.stable_id for item in recommendations])


def valid_rates() -> dict:
    return {
        "overnight": 0.19,
        "off_peak": 0.23,
        "super_off_peak": 0.08,
        "peak": 0.54,
    }


def advisor(advisor_id: str, advisor_type: str) -> Advisor:
    return Advisor(
        advisor_id=advisor_id,
        advisor_type=advisor_type,
        status="ON_TRACK" if advisor_type == "COST" else "GOOD" if advisor_type == "SOLAR" else "CHARGE_NOW" if advisor_type == "EV_CHARGING" else "WATCH",
        headline=f"{advisor_type} headline",
        summary="Summary",
        recommendation="Recommendation",
        estimated_financial_impact=FinancialImpact(billing_period=1.0),
        confidence="High",
        confidence_reason="Test confidence",
        evidence_trail=EvidenceTrail(),
        freshness="Fresh",
        supporting_data_sources=["test"],
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


def insert_reading(
    conn: sqlite3.Connection,
    household_id: str,
    production_kwh: float,
    grid_kwh: float,
    import_cost: float,
) -> None:
    conn.execute(
        """
        INSERT INTO readings (
            household_id,
            measurement_time,
            tariff_bucket,
            production_kwh,
            solar_to_home_kwh,
            solar_to_grid_kwh,
            grid_to_home_w,
            grid_to_home_kwh,
            import_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            household_id,
            "2026-06-27T12:00:00",
            "super_off_peak",
            production_kwh,
            production_kwh / 2,
            production_kwh / 2,
            400.0,
            grid_kwh,
            import_cost,
        ),
    )


def insert_ev_session(
    conn: sqlite3.Connection,
    household_id: str,
    transaction_id: str,
    energy_kwh: float,
) -> None:
    conn.execute(
        """
        INSERT INTO ev_sessions (
            household_id,
            start_time,
            end_time,
            energy_kwh,
            source,
            cost_estimate,
            transaction_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            household_id,
            "2026-06-27T10:00:00",
            "2026-06-27T11:00:00",
            energy_kwh,
            "ocular_export",
            0.65,
            transaction_id,
        ),
    )


if __name__ == "__main__":
    unittest.main()
