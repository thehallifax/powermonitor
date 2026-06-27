import unittest
from dataclasses import dataclass
from datetime import date

from engine.models import Recommendation, RecommendationImpact
from engine.recommendation_engine import (
    RecommendationEngine,
    generate_ev_session_recommendations,
    generate_recommendations,
    generate_recommendations_from_data_engine,
    recommendations_to_decisions,
)


@dataclass
class Settings:
    current_billing_period_start: date = date(2026, 6, 6)
    current_billing_period_end = None
    previous_billing_period_start = None
    previous_billing_period_end = None
    import_rates = {
        "super_off_peak": 0.08,
        "overnight": 0.19,
        "off_peak": 0.23,
        "peak": 0.54,
    }
    ev_baseline_watts = 250.0


class StubDataEngine:
    def get_overnight_baseload_watts(self, start, end):
        return 410.0

    def get_timeline_entries(self, limit=100):
        return []


def base_context():
    return {
        "current": {
            "start": date(2026, 6, 6),
            "end": date(2026, 6, 27),
            "supply_days": 22,
            "reading_days": 22,
            "peak_cost_share_pct": 31.4,
            "buckets": {"peak": {"kwh": 25.32, "cost": 13.64}},
            "self_consumption_pct": 55.2,
            "solar_export_pct": 44.8,
            "ev_charging_kwh": 38.36,
            "ev_charging_cost": 7.44,
            "ev_charging_source": "estimated",
            "net_cost": 71.33,
            "average_daily_net_cost": 3.24,
        },
        "previous": None,
        "import_rates": Settings.import_rates,
        "ev_data_source": "estimated",
        "ev_baseline_watts": 250.0,
        "overnight_baseload_watts": 410.0,
        "timeline_history": None,
        "projected_period_days": 64,
        "projected_period_end": "2026-08-08",
        "projected_net_cost": 207.52,
    }


THRESHOLDS = {
    "peak_cost_share_pct": 25.0,
    "peak_shift_kwh_per_day": 5.0,
    "solar_self_consumption_pct": 60.0,
    "ev_detected_min_kwh": 0.5,
    "baseload_excess_pct": 20.0,
    "high_cost_threshold": 300.0,
    "positive_peak_share_pct": 20.0,
    "positive_self_consumption_pct": 70.0,
    "trend_repeated_event_count": 2.0,
}


class RecommendationEngineTests(unittest.TestCase):
    def test_model_validates_contract(self):
        recommendation = Recommendation(
            stable_id="test",
            category="billing",
            title="Title",
            explanation="Explanation",
            estimated_financial_impact=RecommendationImpact(period=12.5, monthly=17.0),
            confidence="High",
            difficulty="Easy",
            priority_score=140,
            supporting_evidence={"cost": 12.5},
            recommended_action="Act",
        )

        self.assertEqual(recommendation.id, "test")
        self.assertEqual(recommendation.priority_score, 100)
        self.assertFalse(recommendation.dismissible_state.dismissed)
        self.assertEqual(recommendation.as_dict()["estimated_financial_impact"]["monthly"], 17.0)

    def test_model_rejects_unknown_category_or_difficulty(self):
        with self.assertRaises(ValueError):
            Recommendation(
                stable_id="bad",
                category="appliance",
                title="Bad",
                explanation="Bad",
                estimated_financial_impact=RecommendationImpact(),
                confidence="High",
                difficulty="Easy",
                priority_score=1,
                supporting_evidence={},
                recommended_action="Act",
            )

        with self.assertRaises(ValueError):
            Recommendation(
                stable_id="bad",
                category="billing",
                title="Bad",
                explanation="Bad",
                estimated_financial_impact=RecommendationImpact(),
                confidence="High",
                difficulty="Impossible",
                priority_score=1,
                supporting_evidence={},
                recommended_action="Act",
            )

    def test_generate_uses_measurable_observations_and_ranks_deterministically(self):
        recommendations = generate_recommendations(base_context(), THRESHOLDS)
        ids = [item.stable_id for item in recommendations]

        self.assertIn("peak_cost_reduction", ids)
        self.assertIn("solar_self_consumption", ids)
        self.assertIn("ev_charging_optimisation", ids)
        self.assertIn("baseload_monitoring", ids)
        self.assertIn("data_quality_actual_ev_sessions", ids)
        self.assertEqual(
            recommendations,
            sorted(recommendations, key=lambda item: (-item.priority_score, item.stable_id)),
        )
        baseload = next(item for item in recommendations if item.stable_id == "baseload_monitoring")
        self.assertNotIn("hot water", baseload.recommended_action.lower())
        self.assertNotIn("heating", baseload.recommended_action.lower())

    def test_data_quality_recommendation_requires_estimated_ev_data(self):
        context = base_context()
        context["ev_data_source"] = "ocular_export"
        context["current"]["ev_charging_source"] = "ocular_export"

        ids = {item.stable_id for item in generate_recommendations(context, THRESHOLDS)}

        self.assertNotIn("data_quality_actual_ev_sessions", ids)

    def test_data_engine_entrypoint_and_decision_adapter(self):
        recommendations = generate_recommendations_from_data_engine(
            base_context()["current"],
            Settings(),
            StubDataEngine(),
            THRESHOLDS,
        )
        decisions = recommendations_to_decisions(recommendations, expires_at="2026-08-08")

        self.assertTrue(recommendations)
        self.assertEqual(decisions[0].id, recommendations[0].stable_id)
        self.assertEqual(decisions[0].recommendation, recommendations[0].recommended_action)
        self.assertIsNotNone(decisions[0].explanation)

    def test_ev_session_recommendations_use_actual_session_evidence(self):
        sessions = [
            {
                "energy_kwh": 10.0,
                "tariff_breakdown": {
                    "super_off_peak": {"kwh": 9.5, "cost": 0.76},
                    "overnight": {"kwh": 0.0, "cost": 0.0},
                    "off_peak": {"kwh": 0.0, "cost": 0.0},
                    "peak": {"kwh": 0.5, "cost": 0.27},
                },
            }
        ]

        recommendations = generate_ev_session_recommendations(sessions, Settings.import_rates)
        ids = {item.stable_id for item in recommendations}

        self.assertIn("ev_session_low_cost_window", ids)
        self.assertIn("ev_session_peak_shift", ids)


if __name__ == "__main__":
    unittest.main()
