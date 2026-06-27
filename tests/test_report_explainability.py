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
from engine.models import Decision, DecisionExplanation, FinancialImpact


def explained_decision(decision_id: str = "peak_cost_reduction") -> Decision:
    return Decision(
        id=decision_id,
        title="Reduce Peak-period imports",
        summary="Peak usage is expensive.",
        finding="Peak usage accounts for 31.4% of import cost.",
        evidence={"peak_cost": 13.64},
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
