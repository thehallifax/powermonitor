import unittest

from engine.behaviour_engine import BehaviourEngine
from engine.financial_engine import FinancialEngine
from engine.models import (
    Confidence,
    Decision,
    EngineStatus,
    Evidence,
    FinancialImpact,
    Forecast,
    Freshness,
    Insight,
)
from engine.optimisation_engine import OptimisationEngine
from engine.prediction_engine import PredictionEngine


class ModelTests(unittest.TestCase):
    def test_decision_preserves_report_contract(self):
        decision = Decision(
            id="test_decision",
            title="Test decision",
            summary="Summary",
            finding="Finding",
            evidence={"peak_cost": 12.5},
            financial_impact=FinancialImpact(billing_period=4.2),
            recommendation="Act",
            confidence="Medium",
            priority="High",
            expires_at=None,
            data_sources_used=["test"],
            category="Tariffs",
            action_type="load_shift",
        )

        self.assertEqual(decision.severity, "red")
        self.assertEqual(decision.financial_impact.get("billing_period"), 4.2)
        self.assertEqual(decision.metrics["peak_cost"], 12.5)
        self.assertEqual(decision.metrics["billing_period"], 4.2)

        payload = decision.as_dict()
        self.assertEqual(payload["action"], "Act")
        self.assertEqual(payload["severity"], "red")
        self.assertEqual(payload["financial_impact"]["billing_period"], 4.2)
        self.assertEqual(
            set(payload["financial_impact"]),
            {"today", "billing_period", "yearly"},
        )

    def test_model_validation_rejects_unknown_states(self):
        with self.assertRaises(ValueError):
            Confidence(level="Certain")

        with self.assertRaises(ValueError):
            Freshness(state="ancient")

        with self.assertRaises(ValueError):
            EngineStatus(name="test", state="offline")

    def test_insight_and_forecast_accept_evidence_and_confidence(self):
        evidence = Evidence(name="peak_kwh", value=3.4, unit="kWh", source="test")
        confidence = Confidence(level="High", reason="Complete interval data")

        insight = Insight(
            id="test_insight",
            title="Test insight",
            summary="A useful finding",
            evidence=[evidence],
            confidence=confidence,
        )
        forecast = Forecast(
            id="test_forecast",
            title="Test forecast",
            value=42.0,
            unit="AUD",
            evidence=[evidence],
            confidence=confidence,
        )

        self.assertEqual(insight.evidence[0].name, "peak_kwh")
        self.assertEqual(forecast.confidence.level, "High")

    def test_engines_import_and_report_status(self):
        self.assertEqual(FinancialEngine().evaluate({}), [])
        self.assertEqual(BehaviourEngine().evaluate({}), [])
        self.assertEqual(PredictionEngine().forecast({}), [])
        self.assertEqual(OptimisationEngine().optimise([], {}), [])
        self.assertEqual(FinancialEngine().status().state, "ready")


if __name__ == "__main__":
    unittest.main()
