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
        self.assertIn("Priority score", markup)
        self.assertIn("<strong>88</strong>", markup)
        self.assertIn("Tariff", markup)
        self.assertIn("Easy difficulty", markup)
        self.assertIn("$5.73", markup)
        self.assertIn("Supporting evidence", markup)
        self.assertIn("Recommended action", markup)
        self.assertIn("Why?", markup)
        self.assertIn("3 hidden: 1 dismissed, 2 snoozed.", markup)
        self.assertIn("Dismiss", markup)
        self.assertIn("Snooze 1 day", markup)
        self.assertIn("Next bill", markup)
        self.assertIn("Restore", markup)

    def test_recommendation_centre_shows_goal_contributions(self):
        goal = GoalProgress(
            goal=Goal(
                goal_id="peak_reduction",
                title="Peak Reduction",
                outcome="Reduce Peak usage",
                advisor_types=["COST"],
                recommendation_categories=["tariff"],
            ),
            progress_pct=83,
            status=GoalStatus("ON_TRACK", "On track", "Peak reduction is tracking well."),
            confidence="High",
            summary="Peak Reduction is tracking well.",
            supporting_advisors=["Cost"],
            supporting_evidence=["Cost advisor evidence"],
            recommended_action="Keep Peak usage low.",
        )

        markup = report.recommendation_centre_html([explained_decision(category="tariff")], goals=[goal])

        self.assertIn("Contributes to goals", markup)
        self.assertIn("Peak Reduction", markup)

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


if __name__ == "__main__":
    unittest.main()
