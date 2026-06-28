import unittest

from engine.goals_engine import GoalsEngine
from engine.models import Advisor, AdvisorCollection, EvidenceItem, EvidenceTrail, FinancialImpact


def advisor(advisor_type, status, confidence="High"):
    return Advisor(
        advisor_id=f"{advisor_type.lower()}_advisor",
        advisor_type=advisor_type,
        status=status,
        headline=f"{advisor_type} {status}",
        summary=f"{advisor_type} summary",
        recommendation=f"{advisor_type} action",
        estimated_financial_impact=FinancialImpact(billing_period=12.0),
        confidence=confidence,
        confidence_reason=f"{confidence} confidence",
        evidence_trail=EvidenceTrail(
            [
                EvidenceItem(
                    source=f"{advisor_type} source",
                    observation=f"{status} observation",
                    impact="Supports goal progress.",
                    confidence=confidence,
                    freshness="Fresh",
                )
            ]
        ),
        freshness="Fresh",
        supporting_data_sources=[f"{advisor_type} source"],
    )


class GoalsEngineTests(unittest.TestCase):
    def test_evaluates_default_goals_from_advisor_collection(self):
        goals = GoalsEngine().evaluate(
            AdvisorCollection(
                [
                    advisor("COST", "ON_TRACK"),
                    advisor("SOLAR", "GOOD"),
                    advisor("EV_CHARGING", "WAIT", "Medium"),
                    advisor("EFFICIENCY", "WATCH"),
                ]
            )
        )

        by_id = {item.goal.goal_id: item for item in goals}
        self.assertEqual(by_id["bill_target"].progress_pct, 90)
        self.assertEqual(by_id["solar_self_consumption"].progress_pct, 85)
        self.assertEqual(by_id["off_peak_ev_charging"].progress_pct, 78)
        self.assertEqual(by_id["overnight_baseload"].status.state, "WATCH")
        self.assertEqual(by_id["peak_reduction"].confidence, "Medium")

    def test_unknown_goal_when_supporting_advisor_has_no_usable_evidence(self):
        goals = GoalsEngine().evaluate(AdvisorCollection([advisor("SOLAR", "UNKNOWN", "Low")]))
        solar = next(item for item in goals if item.goal.goal_id == "solar_self_consumption")

        self.assertIsNone(solar.progress_pct)
        self.assertEqual(solar.status.state, "UNKNOWN")
        self.assertEqual(solar.confidence, "Low")

    def test_goal_progress_serializes(self):
        progress = GoalsEngine().evaluate(AdvisorCollection([advisor("COST", "HIGH")]))[0]
        data = progress.as_dict()

        self.assertEqual(data["goal"]["goal_id"], "bill_target")
        self.assertIn("status", data)
        self.assertIn("supporting_evidence", data)


if __name__ == "__main__":
    unittest.main()
