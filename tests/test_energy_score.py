import unittest
from dataclasses import dataclass

from engine.models import Decision, FinancialImpact
from presentation.energy_score import build_energy_score


@dataclass
class Summary:
    peak_cost_share_pct: float
    self_consumption_pct: float
    ev_charging_kwh: float = 8.0
    ev_charging_source: str = "ocular_export"


def decision(
    decision_id: str,
    priority: str = "Low",
    action_type: str = "maintain",
    confidence: str = "High",
) -> Decision:
    return Decision(
        id=decision_id,
        title=decision_id,
        summary=f"{decision_id} summary",
        finding=f"{decision_id} finding",
        evidence={},
        financial_impact=FinancialImpact(),
        recommendation=f"{decision_id} recommendation",
        confidence=confidence,
        priority=priority,
        expires_at=None,
        data_sources_used=["SolarEdge interval data"],
        category="General",
        action_type=action_type,
    )


class EnergyScoreTests(unittest.TestCase):
    def test_excellent_score_band_with_high_confidence(self):
        score = build_energy_score(
            Summary(peak_cost_share_pct=10.0, self_consumption_pct=82.0),
            [decision("trend_recurring_ev_charging_win")],
            {"confidence": "High", "confidence_note": "Complete data"},
            {"savings": 12.0},
        )

        self.assertEqual(score.label, "Excellent")
        self.assertGreaterEqual(score.score, 85)
        self.assertEqual(score.confidence, "High")
        self.assertIn("Potential score", score.summary)

    def test_fair_or_poor_score_penalises_negative_factors_and_estimated_data(self):
        score = build_energy_score(
            Summary(
                peak_cost_share_pct=42.0,
                self_consumption_pct=38.0,
                ev_charging_source="estimated",
            ),
            [decision("trend_repeated_estimated_data_warning", "Medium", "data_quality", "Medium")],
            {"confidence": "Low", "confidence_note": "Sparse data"},
            {"savings": -6.0},
        )

        self.assertIn(score.label, {"Poor", "Fair"})
        self.assertGreater(score.potential_score, score.score)
        self.assertEqual(score.confidence, "Low")
        self.assertTrue(score.top_negative_factors)
        self.assertIn("EV source: estimated", score.explanation.evidence_used)

    def test_good_score_band_with_medium_confidence(self):
        score = build_energy_score(
            Summary(
                peak_cost_share_pct=30.0,
                self_consumption_pct=66.0,
                ev_charging_source="estimated",
            ),
            [decision("solar_self_consumption", "Medium", "load_shift", "Medium")],
            {"confidence": "Medium", "confidence_note": "Partial EV data"},
            {"savings": 0.0},
        )

        self.assertEqual(score.label, "Good")
        self.assertEqual(score.confidence, "Medium")
        self.assertIn("Data completeness summary", score.data_sources_used)


if __name__ == "__main__":
    unittest.main()
