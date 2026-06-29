import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_ROOT = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import collector.report as report
from engine.advisor_presenter import attach_decision_to_advisors
from engine.decision_engine import (
    DecisionConfidence,
    DecisionEvidence,
    DecisionOutcome,
    DecisionPriority,
    DecisionReason,
    DecisionStatus,
    DecisionType,
    HouseholdDecision,
)
from engine.decision_pipeline import DecisionPipeline
from engine.goals_engine import GoalsEngine
from engine.models import Advisor, AdvisorCollection, EvidenceTrail, FinancialImpact
from engine.notification_engine import NotificationEngine


LOCAL = ZoneInfo("Australia/Perth")


def household_decision(decision_id="decision_1", decision_type=DecisionType.WAIT_FOR_CHEAPER_WINDOW):
    return HouseholdDecision(
        decision_id=decision_id,
        household_id="default_household",
        decision_type=decision_type,
        priority=DecisionPriority.HIGH,
        confidence=DecisionConfidence.HIGH,
        status=DecisionStatus.ACTIVE,
        created_at="2026-06-30T12:00:00+08:00",
        reasons=[DecisionReason("decision_owned_action", "Decision Engine owns the household action.")],
        evidence=[DecisionEvidence("DecisionEngine", "decision_type", decision_type.value, DecisionConfidence.HIGH)],
        outcome=DecisionOutcome("active", "Use the Decision Pipeline action."),
        financial_impact={"today": 1.0, "billing_period": 10.0, "yearly": 120.0},
        data_sources_used=["DecisionEngine"],
    )


def advisor(advisor_type="EV_CHARGING", recommendation="Advisor native action"):
    return Advisor(
        advisor_id=advisor_type.lower(),
        advisor_type=advisor_type,
        status="WAIT",
        headline="Advisor explanation",
        summary="Advisor explains evidence.",
        recommendation=recommendation,
        estimated_financial_impact=FinancialImpact(today=1.0),
        confidence="High",
        confidence_reason="Evidence explanation only.",
        evidence_trail=EvidenceTrail(),
        freshness="Fresh",
        supporting_data_sources=["test"],
    )


class LegacyRecommendationRetirementTests(unittest.TestCase):
    def test_legacy_advisor_notification_fallback_is_disabled_by_default(self):
        readiness = NotificationEngine().build(
            household_state_snapshot={"household_id": "default_household"},
            advisors=AdvisorCollection([advisor(recommendation="Legacy advisor action")]),
            evidence_quality="HIGH",
            now=datetime(2026, 6, 30, 12, tzinfo=LOCAL),
        )

        self.assertEqual(readiness.candidates, [])

    def test_legacy_advisor_attachment_uses_decision_pipeline_action(self):
        decision = household_decision()
        pipeline = DecisionPipeline().present([decision])
        migrated = attach_decision_to_advisors([advisor(recommendation="Legacy advisor action")], [decision])

        self.assertEqual(migrated[0].decision_id, pipeline.recommendations[0].decision_id)
        self.assertEqual(migrated[0].recommendation, pipeline.recommendations[0].recommended_action)
        self.assertNotEqual(migrated[0].recommendation, "Legacy advisor action")

    def test_unmatched_advisor_is_evidence_only_not_action_guidance(self):
        migrated = attach_decision_to_advisors([advisor("SOLAR", "Run a legacy solar action")], [])

        self.assertEqual(
            migrated[0].recommendation,
            "Advisor evidence only; use Decision Pipeline recommendations for actions.",
        )

    def test_goals_do_not_emit_advisor_native_actions(self):
        goals = GoalsEngine().evaluate(AdvisorCollection([advisor("EV_CHARGING", "Legacy EV action")]))

        self.assertTrue(goals)
        self.assertTrue(
            all(goal.recommended_action.startswith("Use active Decision Pipeline") for goal in goals)
        )

    def test_api_recommendations_are_decision_native(self):
        payload = report.dashboard_data()
        recommendations = payload["recommendations"]

        self.assertTrue(recommendations)
        self.assertTrue(all(item["decision_id"] for item in recommendations))
        self.assertTrue(all(item["decision"] for item in recommendations))
        self.assertTrue(all(item["context"] is not None for item in recommendations))
        self.assertTrue(all(item["behaviour"] is not None for item in recommendations))
        self.assertFalse(any("stable_id" in item for item in recommendations))


if __name__ == "__main__":
    unittest.main()
