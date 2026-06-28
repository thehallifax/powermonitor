import unittest
from dataclasses import dataclass, field

from engine.efficiency_advisor import EfficiencyAdvisor
from engine.models import Decision, DecisionTimeline, FinancialImpact, TimelineEntry


@dataclass
class Summary:
    supply_days: int = 20
    reading_days: int = 18
    intervals: int = 100
    average_daily_grid_import_kwh: float = 12.0
    grid_import_kwh: float = 240.0
    import_cost: float = 72.0
    buckets: dict = field(default_factory=lambda: {"peak": {"cost": 12.0}})


@dataclass
class Settings:
    ev_baseline_watts: float = 250.0
    recommendation_thresholds: dict = field(default_factory=lambda: {"baseload_excess_pct": 20.0})


def recommendation():
    return Decision(
        id="baseload_monitoring",
        title="Review overnight baseload",
        summary="Baseload is high.",
        finding="Baseload is high.",
        evidence={},
        financial_impact=FinancialImpact(),
        recommendation="Review measured overnight load patterns.",
        confidence="Medium",
        priority="Medium",
        expires_at=None,
        data_sources_used=["SolarEdge interval data"],
        category="Behaviour",
        action_type="investigate",
    )


def timeline():
    return DecisionTimeline(
        [
            TimelineEntry(
                timestamp="2026-06-20T12:00:00+08:00",
                title="Baseload reviewed",
                summary="Baseload was reviewed.",
                financial_impact=None,
                confidence="Medium",
                evidence_sources=["SolarEdge interval data"],
                event_type="baseload_review",
            )
        ]
    )


class EfficiencyAdvisorTests(unittest.TestCase):
    def test_inefficient_when_baseload_exceeds_threshold(self):
        advisor = EfficiencyAdvisor().advise(
            summary=Summary(),
            previous_summary=Summary(average_daily_grid_import_kwh=10.0),
            settings=Settings(),
            recommendations=[recommendation()],
            timeline=timeline(),
            health={"status": "ok", "report_age_minutes": 5},
            overnight_baseload_watts=420.0,
            comparison={"deltas": {"grid_import_kwh": 40.0}},
            trends=[object(), object()],
        )

        self.assertEqual(advisor.status, "INEFFICIENT")
        self.assertIn("higher than expected", advisor.headline)
        self.assertGreater(advisor.estimated_financial_impact.billing_period, 0)
        sources = [item.source for item in advisor.evidence_trail.items]
        self.assertIn("Overnight baseload", sources)
        self.assertIn("Recommendation context", sources)
        self.assertIn("Timeline evidence", sources)
        self.assertEqual(advisor.evidence_summary.evidence_quality.value, "HIGH")

    def test_excellent_when_daily_import_improves(self):
        advisor = EfficiencyAdvisor().advise(
            summary=Summary(average_daily_grid_import_kwh=8.0),
            previous_summary=Summary(average_daily_grid_import_kwh=10.0),
            settings=Settings(),
            recommendations=[],
            timeline=None,
            health={"status": "ok", "report_age_minutes": 5},
            overnight_baseload_watts=220.0,
            comparison={"deltas": {"grid_import_kwh": -40.0}},
            trends=[],
        )

        self.assertEqual(advisor.status, "EXCELLENT")
        self.assertIn("20%", advisor.headline)
        self.assertEqual(advisor.estimated_financial_impact.billing_period, 0.0)

    def test_unknown_without_overnight_baseload(self):
        advisor = EfficiencyAdvisor().advise(
            summary=Summary(intervals=0),
            previous_summary=None,
            settings=Settings(),
            recommendations=[],
            timeline=None,
            health={"status": "ok", "report_age_minutes": 5},
            overnight_baseload_watts=0.0,
            comparison=None,
            trends=[],
        )

        self.assertEqual(advisor.status, "UNKNOWN")
        self.assertEqual(advisor.confidence, "Low")
        self.assertIsNone(advisor.estimated_financial_impact)
        self.assertEqual(advisor.evidence_summary.evidence_quality.value, "LOW")
        self.assertIn("Baseload estimation is incomplete", advisor.evidence_summary.reason)


if __name__ == "__main__":
    unittest.main()
