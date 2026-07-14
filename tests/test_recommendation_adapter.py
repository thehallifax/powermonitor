import json
import unittest
from pathlib import Path
import sys
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
from engine.recommendation_adapter import RecommendationAdapter, adapt_recommendations


def household_decision(
    decision_type: DecisionType,
    *,
    decision_id: str,
    priority: DecisionPriority = DecisionPriority.HIGH,
    confidence: DecisionConfidence = DecisionConfidence.HIGH,
    effect: str = "Take the recommended household action.",
    impact: Optional[dict] = None,
) -> HouseholdDecision:
    return HouseholdDecision(
        decision_id=decision_id,
        household_id="household_a",
        decision_type=decision_type,
        priority=priority,
        confidence=confidence,
        status=DecisionStatus.ACTIVE,
        created_at="2026-06-29T08:00:00+08:00",
        reasons=[
            DecisionReason(
                code=f"{decision_type.value.lower()}_reason",
                summary=f"{decision_type.value.replace('_', ' ').title()} is supported by current household evidence.",
            )
        ],
        evidence=[
            DecisionEvidence(
                source="DecisionEngine",
                signal="decision_type",
                value=decision_type.value,
                quality=confidence,
                observed_at="2026-06-29T08:00:00+08:00",
            )
        ],
        outcome=DecisionOutcome(status="active", expected_effect=effect),
        financial_impact=impact or {"today": 1.5, "billing_period": 12.0, "yearly": 144.0},
        data_sources_used=["DataEngine", "DecisionEngine"],
        related_state_change_ids=["state_1"],
    )


class RecommendationAdapterTests(unittest.TestCase):
    def test_charging_recommendation_presentation(self):
        recommendation = RecommendationAdapter().adapt_one(
            household_decision(
                DecisionType.WAIT_FOR_CHEAPER_WINDOW,
                decision_id="charge_1",
                effect="Wait for the next lower-cost tariff window.",
            )
        )

        self.assertEqual(recommendation.decision_id, "charge_1")
        self.assertEqual(recommendation.category.label, "EV Charging")
        self.assertEqual(recommendation.icon, "clock")
        self.assertEqual(recommendation.recommended_action, "Wait for the next lower-cost tariff window.")
        self.assertEqual(recommendation.decision["type"], "WAIT_FOR_CHEAPER_WINDOW")

    def test_billing_recommendation_presentation(self):
        recommendation = RecommendationAdapter().adapt_one(
            household_decision(
                DecisionType.REVIEW_BILLING,
                decision_id="bill_1",
                effect="Review the current bill trajectory before the period closes.",
            )
        )

        self.assertEqual(recommendation.category.label, "Bills")
        self.assertEqual(recommendation.icon, "receipt")
        self.assertEqual(recommendation.title, "Current bill is tracking above previous periods")

    def test_monitoring_recommendation_presentation(self):
        recommendation = RecommendationAdapter().adapt_one(
            household_decision(
                DecisionType.MONITOR_USAGE,
                decision_id="monitor_1",
                effect="Keep monitoring this pattern before taking action.",
            )
        )

        self.assertEqual(recommendation.category.label, "Monitoring")
        self.assertEqual(recommendation.icon, "activity")
        self.assertEqual(recommendation.recommended_action, "Keep monitoring this pattern before taking action.")

    def test_anomaly_investigation_presentation(self):
        decision = household_decision(
            DecisionType.INVESTIGATE_ANOMALY,
            decision_id="anomaly_1",
            effect="Investigate the active anomaly using its evidence trail.",
        )
        recommendation = RecommendationAdapter().adapt_one(decision)

        self.assertEqual(recommendation.category.label, "Monitoring")
        self.assertEqual(recommendation.title, "Overnight usage appears higher than normal")
        self.assertEqual(recommendation.recommended_action, "Check overnight loads and look for anything running longer than usual.")
        self.assertEqual(recommendation.icon, "search")
        self.assertEqual(recommendation.evidence[0].source, "DecisionEngine")

    def test_no_action_recommendation_presentation(self):
        recommendation = RecommendationAdapter().adapt_one(
            household_decision(
                DecisionType.NO_ACTION,
                decision_id="no_action_1",
                priority=DecisionPriority.LOW,
                confidence=DecisionConfidence.MEDIUM,
                effect="Continue monitoring current household state.",
                impact={"today": None, "billing_period": None, "yearly": None},
            )
        )

        self.assertEqual(recommendation.category.label, "General")
        self.assertEqual(recommendation.severity, "green")
        self.assertEqual(recommendation.recommended_action, "Continue monitoring current household state.")

    def test_priority_ordering(self):
        recommendations = adapt_recommendations([
            household_decision(DecisionType.NO_ACTION, decision_id="low", priority=DecisionPriority.LOW),
            household_decision(DecisionType.INVESTIGATE_ANOMALY, decision_id="critical", priority=DecisionPriority.CRITICAL),
            household_decision(DecisionType.MONITOR_USAGE, decision_id="medium", priority=DecisionPriority.MEDIUM),
        ])

        self.assertEqual([item.decision_id for item in recommendations], ["critical", "medium"])

    def test_serialization_retains_decision_lineage(self):
        recommendation = RecommendationAdapter().adapt_one(
            household_decision(DecisionType.REVIEW_BILLING, decision_id="bill_2"),
            advisor_explanation={"advisor_id": "cost"},
            household_state={"household_id": "household_a", "state": "ready"},
        )

        payload = recommendation.as_dict()
        json.dumps(payload)

        self.assertEqual(payload["decision_id"], "bill_2")
        self.assertEqual(payload["decision"]["decision_id"], "bill_2")
        self.assertEqual(payload["advisor_explanation"]["advisor_id"], "cost")
        self.assertEqual(payload["state_change_ids"], ["state_1"])
        self.assertEqual(payload["household_state"]["household_id"], "household_a")
        self.assertEqual(payload["evidence"][0]["signal"], "decision_type")
        self.assertIn("what_happened", payload)
        self.assertIn("why_we_think_this", payload)
        self.assertIn("what_to_check", payload)
        self.assertIn("if_ignored", payload)

    def test_insufficient_data_recommendation_explains_no_recommendation(self):
        recommendation = RecommendationAdapter().adapt_one(
            household_decision(
                DecisionType.DATA_INSUFFICIENT,
                decision_id="data_1",
                priority=DecisionPriority.MEDIUM,
                confidence=DecisionConfidence.LOW,
                effect="We don't yet have enough information to make a recommendation.",
                impact={"today": None, "billing_period": None, "yearly": None},
            )
        )

        self.assertEqual(
            recommendation.recommended_action,
            "We don't yet have enough information to make a recommendation.",
        )
        self.assertIn("not yet have enough verified household data", recommendation.what_happened)
        self.assertIn("missing, stale, estimated, or incomplete", recommendation.why_we_think_this)
        self.assertIn("live data collection", recommendation.what_to_check)

    def test_baseload_anomaly_recommendation_uses_household_behaviour(self):
        recommendation = RecommendationAdapter().adapt_one(
            household_decision(
                DecisionType.INVESTIGATE_ANOMALY,
                decision_id="baseload_1",
                effect="Investigate the active anomaly using its evidence trail.",
            ),
            behaviour={
                "baseload": {
                    "overnight_baseload_watts": 420.0,
                    "normal_variance_watts": 35.0,
                }
            },
        )

        self.assertIn("420 W", recommendation.what_happened)
        self.assertIn("learned overnight baseline", recommendation.why_we_think_this)
        self.assertIn("pool pump", recommendation.what_to_check)


if __name__ == "__main__":
    unittest.main()
