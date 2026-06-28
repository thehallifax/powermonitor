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


if __name__ == "__main__":
    unittest.main()
