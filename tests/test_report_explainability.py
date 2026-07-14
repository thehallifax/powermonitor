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
from engine.models import Decision, DecisionExplanation, FinancialImpact, Goal, GoalProgress, GoalStatus
from engine.recommendation_adapter import (
    RecommendationCategory,
    RecommendationEvidence,
    RecommendationPresentation,
    RecommendationPriority,
)


def explained_decision(
    decision_id: str = "peak_cost_reduction",
    title: str = "Reduce Peak-period imports",
    priority_score: int = 72,
    category: str = "tariff",
) -> Decision:
    return Decision(
        id=decision_id,
        title=title,
        summary="Peak usage is expensive.",
        finding="Peak usage accounts for 31.4% of import cost.",
        evidence={
            "peak_cost": 13.64,
            "priority_score": priority_score,
            "recommendation_category": category,
            "estimated_monthly_impact": 5.73,
            "difficulty": "Easy",
        },
        financial_impact=FinancialImpact(billing_period=4.20),
        recommendation="Move flexible load into the super off-peak window.",
        confidence="Medium",
        priority="Medium",
        expires_at="2026-06-30",
        data_sources_used=["SolarEdge interval data", "Tariff settings"],
        category="Tariffs",
        action_type="load_shift",
        explanation=DecisionExplanation(
            why_this_matters="This points to usage that may cost more than it needs to.",
            evidence_used=["Peak cost: 13.64"],
            financial_impact="$4.20 this billing period",
            confidence_reason="Medium confidence based on available interval data.",
            data_sources_used=["SolarEdge interval data", "Tariff settings"],
            freshness_completeness_note="Decision expires at 2026-06-30.",
        ),
    )


class ReportExplainabilityTests(unittest.TestCase):
    def test_recommendation_cards_render_explanations(self):
        markup = report.recommendations_html([explained_decision()])

        self.assertIn("<summary>Why?</summary>", markup)
        self.assertIn("Why this matters", markup)
        self.assertIn("Evidence used", markup)
        self.assertIn("Financial impact", markup)
        self.assertIn("Confidence reason", markup)
        self.assertIn("Data sources used", markup)
        self.assertIn("Freshness/completeness", markup)

    def test_top_action_cards_render_explanations(self):
        markup = report.top_actions_html([explained_decision()])

        self.assertIn("<summary>Why?</summary>", markup)
        self.assertIn("Peak cost: 13.64", markup)
        self.assertIn("SolarEdge interval data, Tariff settings", markup)

    def test_recommendation_centre_sorts_by_priority_and_renders_engine_fields(self):
        low = explained_decision(
            "solar_self_consumption",
            title="Use more solar at home",
            priority_score=42,
            category="solar",
        )
        low.evidence["lifecycle_state"] = "dismissed"
        high = explained_decision(
            "peak_cost_reduction",
            title="Reduce Peak-period imports",
            priority_score=88,
            category="tariff",
        )

        markup = report.recommendation_centre_html(
            [high],
            {"dismissed": 1, "snoozed_until": 2},
            [low],
        )

        self.assertLess(
            markup.index("Reduce Peak-period imports"),
            markup.index("Use more solar at home"),
        )
        self.assertIn("Recommendation centre", markup)
        self.assertIn("$4.20", markup)
        self.assertIn("Why?", markup)
        self.assertIn("3 hidden: 1 dismissed, 2 snoozed.", markup)
        self.assertIn("Dismiss", markup)
        self.assertIn("Snooze 1 day", markup)
        self.assertIn("Next bill", markup)
        self.assertIn("Restore", markup)
        self.assertNotIn("Priority score", markup)
        self.assertNotIn("Unable to estimate", markup)

    def test_recommendation_centre_surfaces_low_confidence_learning_items(self):
        decision = explained_decision(category="tariff")
        decision.confidence = "Low"

        markup = report.recommendation_centre_html([decision])

        self.assertIn("Things PowerMonitor is still learning", markup)
        self.assertIn("PowerMonitor is still learning this pattern", markup)

    def test_serialized_decision_includes_explanation_for_api_outputs(self):
        payload = explained_decision().as_dict()

        self.assertEqual(
            set(payload["explanation"]),
            {
                "why_this_matters",
                "evidence_used",
                "financial_impact",
                "confidence_reason",
                "data_sources_used",
                "freshness_completeness_note",
            },
        )
        self.assertEqual(payload["explanation"]["financial_impact"], "$4.20 this billing period")

    def test_recommendation_wording_softens_low_confidence_and_missing_live_data(self):
        decision = explained_decision()
        decision.confidence = "Low"
        decision.explanation = DecisionExplanation(
            why_this_matters="Live solar output cannot be verified.",
            evidence_used=["Solar interval evidence is unavailable."],
            financial_impact="Unable to estimate",
            confidence_reason="Low confidence because solar interval evidence is unavailable.",
            data_sources_used=["Tariff settings"],
            freshness_completeness_note="Live readings are missing.",
        )

        self.assertEqual(
            report.recommendation_confidence_note(decision),
            "Low confidence because live solar readings are unavailable.",
        )

    def test_anomaly_wording_is_presented_as_potential_issue(self):
        decision = explained_decision(
            decision_id="anomaly_1",
            title="Investigate unusual household behaviour",
            category="monitoring",
        )

        self.assertEqual(report.recommendation_title(decision), "Overnight usage appears higher than normal")

    def test_recommendation_centre_answers_homeowner_rationale_questions(self):
        markup = report.recommendation_centre_html([explained_decision()])

        self.assertIn("<strong>Action</strong>", markup)
        self.assertIn("<strong>Why</strong>", markup)
        self.assertIn("<summary>Why?</summary>", markup)

    def test_recommendation_centre_renders_presentation_rationale_fields(self):
        recommendation = RecommendationPresentation(
            recommendation_id="decision:monitor_usage",
            decision_id="monitor_usage",
            household_id="default_household",
            title="Check unusual usage",
            summary="Usage changed overnight.",
            recommended_action="Check overnight loads.",
            rationale="Overnight usage is higher than usual.",
            what_happened="Your overnight electricity use was higher than usual.",
            why_we_think_this="This is based on recent household readings.",
            what_to_check="Check pool pumps, hot water, and appliances left running.",
            if_ignored="The extra usage may continue into future bills.",
            confidence="Medium",
            evidence=[
                RecommendationEvidence(
                    source="household_state",
                    signal="overnight_load",
                    value="Higher than usual",
                    quality="MEDIUM",
                )
            ],
            urgency=RecommendationPriority("medium", "Medium", 50),
            category=RecommendationCategory("billing", "Bills", "*"),
            icon="*",
            display_priority=72,
            financial_impact=FinancialImpact(billing_period=4.2),
            decision={"decision_id": "monitor_usage", "type": "REVIEW_BILLING"},
        )

        markup = report.recommendation_centre_html([recommendation])

        self.assertIn("Check overnight loads.", markup)
        self.assertIn("Overnight usage is higher than usual.", markup)
        self.assertIn("This is based on recent household readings.", markup)


if __name__ == "__main__":
    unittest.main()
