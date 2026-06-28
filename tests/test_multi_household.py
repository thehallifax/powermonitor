import sqlite3
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report
from engine.briefing_engine import BriefingEngine
from engine.data_engine import DataEngine
from engine.goals_engine import GoalsEngine
from engine.data_engine import DEFAULT_HOUSEHOLD_ID
from engine.models import (
    Account,
    Advisor,
    AdvisorCollection,
    EvidenceTrail,
    FinancialImpact,
    Household,
    HouseholdCapability,
    HouseholdContext,
    HouseholdMembership,
)


def create_readings_table(conn):
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


def advisor():
    return Advisor(
        advisor_id="cost_advisor",
        advisor_type="COST",
        status="ON_TRACK",
        headline="Bill is on track",
        summary="The household is tracking well.",
        recommendation="Stay the course.",
        estimated_financial_impact=FinancialImpact(billing_period=10.0),
        confidence="High",
        confidence_reason="High confidence test advisor.",
        evidence_trail=EvidenceTrail(),
        freshness="Fresh",
        supporting_data_sources=["Test"],
    )


class MultiHouseholdFoundationTests(unittest.TestCase):
    def test_platform_models_serialize_household_context(self):
        context = HouseholdContext(
            account=Account("acct_1", "Beta account", "local"),
            household=Household(
                "home_1",
                "Beta home",
                "Australia/Perth",
                [HouseholdCapability("solar", source="test")],
            ),
            membership=HouseholdMembership("acct_1", "home_1", "owner"),
        )

        data = context.as_dict()

        self.assertEqual(data["account"]["account_id"], "acct_1")
        self.assertEqual(data["household"]["household_id"], "home_1")
        self.assertEqual(data["household"]["capabilities"][0]["capability_id"], "solar")
        self.assertEqual(data["membership"]["role"], "owner")

    def test_data_engine_seeds_default_household_context(self):
        conn = sqlite3.connect(":memory:")
        try:
            create_readings_table(conn)
            engine = DataEngine(connection=conn)
            engine.validate_database()

            context = engine.get_household_context()

            self.assertEqual(context.household.household_id, "default_household")
            self.assertTrue(context.household.is_default)
            self.assertEqual(context.membership.account_id, "local_account")
            self.assertIn("billing", [item.capability_id for item in context.household.capabilities])
        finally:
            conn.close()

    def test_orchestration_models_are_household_scoped_without_changing_advisors(self):
        context = report.default_household_context()
        collection = AdvisorCollection([advisor()], household=context.household)

        briefing = BriefingEngine(context).build(collection)
        goals = GoalsEngine(household_context=context).evaluate(collection)

        self.assertEqual(collection.as_dict()["household"]["household_id"], "default_household")
        self.assertEqual(briefing.positive_outcomes[0].advisor_id, "cost_advisor")
        self.assertEqual(goals[0].goal.goal_id, "bill_target")

    def test_migration_assigns_legacy_rows_to_default_household(self):
        conn = sqlite3.connect(":memory:")
        try:
            create_readings_table(conn)
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
                ) VALUES ('2026-06-27T12:00:00', 'super_off_peak', 1, 1, 0, 100, 0.025, 0.01)
                """
            )
            conn.execute(
                """
                CREATE TABLE ev_sessions (
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    energy_kwh REAL NOT NULL,
                    source TEXT NOT NULL,
                    cost_estimate REAL,
                    transaction_id TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO ev_sessions (
                    start_time, end_time, energy_kwh, source, cost_estimate, transaction_id
                ) VALUES ('2026-06-27T10:00:00', '2026-06-27T11:00:00', 7.5, 'ocular_export', 0.65, 'legacy_ev')
                """
            )
            conn.execute(
                """
                CREATE TABLE timeline_entries (
                    event_key TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    financial_impact TEXT,
                    confidence TEXT NOT NULL,
                    evidence_sources_json TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO timeline_entries (
                    event_key, timestamp, title, summary, financial_impact,
                    confidence, evidence_sources_json, event_type, created_at, updated_at
                ) VALUES (
                    'legacy_timeline', '2026-06-27', 'Legacy event', 'Legacy summary',
                    NULL, 'High', '[]', 'billing_period', '2026-06-27', '2026-06-27'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE recommendation_lifecycle (
                    stable_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT 'active',
                    snoozed_until TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO recommendation_lifecycle (
                    stable_id, state, reason, created_at, updated_at
                ) VALUES ('peak_cost_reduction', 'dismissed', 'legacy', '2026-06-27', '2026-06-27')
                """
            )

            engine = DataEngine(connection=conn)
            engine.validate_database()

            self.assertEqual(
                conn.execute("SELECT DISTINCT household_id FROM readings").fetchall(),
                [(DEFAULT_HOUSEHOLD_ID,)],
            )
            self.assertEqual(
                conn.execute("SELECT DISTINCT household_id FROM ev_sessions").fetchall(),
                [(DEFAULT_HOUSEHOLD_ID,)],
            )
            self.assertEqual(
                conn.execute("SELECT DISTINCT household_id FROM timeline_entries").fetchall(),
                [(DEFAULT_HOUSEHOLD_ID,)],
            )
            self.assertEqual(
                conn.execute("SELECT household_id, state FROM recommendation_lifecycle").fetchall(),
                [(DEFAULT_HOUSEHOLD_ID, "dismissed")],
            )
        finally:
            conn.close()

    def test_household_scoped_queries_do_not_leak_between_households(self):
        conn = sqlite3.connect(":memory:")
        try:
            create_readings_table(conn)
            engine = DataEngine(connection=conn)
            engine.validate_database()
            context_a = household_context("household_a")
            context_b = household_context("household_b")
            insert_scoped_reading(conn, "household_a", "2026-06-27T12:00:00", 0.10)
            insert_scoped_reading(conn, "household_b", "2026-06-27T12:00:00", 0.90)
            insert_scoped_session(conn, "household_a", "session_a", 7.5)
            insert_scoped_session(conn, "household_b", "session_b", 22.0)

            engine_a = DataEngine(connection=conn, household_context=context_a)
            engine_b = DataEngine(connection=conn, household_context=context_b)
            entry = timeline_entry("Shared event")
            engine_a.persist_timeline_entries([entry])
            engine_b.persist_timeline_entries([entry])
            engine_a.set_recommendation_lifecycle("peak_cost_reduction", "dismissed", reason="A")
            engine_b.set_recommendation_lifecycle("peak_cost_reduction", "snoozed_until", snoozed_until="2999-01-01", reason="B")

            self.assertEqual(
                [row["grid_to_home_kwh"] for row in engine_a.get_grid_intervals(date_from(), date_from())],
                [0.10],
            )
            self.assertEqual(
                [row["grid_to_home_kwh"] for row in engine_b.get_grid_intervals(date_from(), date_from())],
                [0.90],
            )
            self.assertEqual(engine_a.get_ev_sessions(date_from(), date_from(), ["ocular_export"])[0]["energy_kwh"], 7.5)
            self.assertEqual(engine_b.get_ev_sessions(date_from(), date_from(), ["ocular_export"])[0]["energy_kwh"], 22.0)
            self.assertEqual(engine_a.get_recommendation_lifecycle()["peak_cost_reduction"]["state"], "dismissed")
            self.assertEqual(engine_b.get_recommendation_lifecycle()["peak_cost_reduction"]["state"], "snoozed_until")
            self.assertEqual(len(engine_a.get_timeline_entries().entries), 1)
            self.assertEqual(len(engine_b.get_timeline_entries().entries), 1)
        finally:
            conn.close()


def household_context(household_id: str) -> HouseholdContext:
    return HouseholdContext(
        account=Account(f"acct_{household_id}", f"Account {household_id}", "local"),
        household=Household(
            household_id,
            f"Home {household_id}",
            "Australia/Perth",
            [HouseholdCapability("solar", source="test")],
        ),
        membership=HouseholdMembership(f"acct_{household_id}", household_id, "owner"),
    )


def date_from():
    from datetime import date

    return date(2026, 6, 27)


def insert_scoped_reading(conn, household_id: str, measurement_time: str, grid_kwh: float) -> None:
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
        ) VALUES (?, ?, 'super_off_peak', 0, 0, 0, 100, ?, 0)
        """,
        (household_id, measurement_time, grid_kwh),
    )


def insert_scoped_session(conn, household_id: str, transaction_id: str, energy_kwh: float) -> None:
    conn.execute(
        """
        INSERT INTO ev_sessions (
            household_id,
            transaction_id,
            start_time,
            end_time,
            energy_kwh,
            source,
            cost_estimate,
            super_off_peak_kwh,
            super_off_peak_cost
        ) VALUES (?, ?, '2026-06-27T10:00:00', '2026-06-27T11:00:00', ?, 'ocular_export', 0, ?, 0)
        """,
        (household_id, transaction_id, energy_kwh, energy_kwh),
    )


def timeline_entry(title: str):
    from engine.models import TimelineEntry

    return TimelineEntry(
        timestamp="2026-06-27",
        title=title,
        summary="Household-scoped event.",
        financial_impact=None,
        confidence="High",
        evidence_sources=["test"],
        event_type="billing_period",
    )


if __name__ == "__main__":
    unittest.main()
