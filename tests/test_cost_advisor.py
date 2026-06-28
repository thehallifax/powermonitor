import unittest
from dataclasses import dataclass, field
from datetime import date

from engine.cost_advisor import CostAdvisor
from engine.models import DecisionTimeline, TimelineEntry


@dataclass
class Summary:
    start: date = date(2026, 6, 1)
    end: date = date(2026, 6, 20)
    net_cost: float = 80.0
    average_daily_net_cost: float = 4.0
    supply_days: int = 20
    reading_days: int = 18
    intervals: int = 100
    grid_import_kwh: float = 200.0
    solar_generated_kwh: float = 70.0
    ev_charging_kwh: float = 25.0
    ev_charging_cost: float = 5.0
    peak_cost_share_pct: float = 20.0
    buckets: dict = field(default_factory=lambda: {"peak": {"cost": 12.0}})


@dataclass
class Settings:
    current_billing_period_start: date = date(2026, 6, 1)
    current_billing_period_end: date = date(2026, 6, 30)
    previous_billing_period_start: date = date(2026, 5, 1)
    previous_billing_period_end: date = date(2026, 5, 30)


def timeline():
    return DecisionTimeline(
        [
            TimelineEntry(
                timestamp="2026-06-20T12:00:00+08:00",
                title="Bill reviewed",
                summary="Bill trend was reviewed.",
                financial_impact=None,
                confidence="High",
                evidence_sources=["Financial Engine"],
                event_type="cost_review",
            )
        ]
    )


class CostAdvisorTests(unittest.TestCase):
    def test_improving_when_projected_below_previous_period(self):
        advisor = CostAdvisor().advise(
            current_summary=Summary(average_daily_net_cost=3.0),
            previous_summary=Summary(net_cost=120.0),
            settings=Settings(),
            recommendations=[],
            timeline=timeline(),
            health={"status": "ok", "report_age_minutes": 4},
            comparison={"deltas": {"grid_import_kwh": -20.0, "solar_generated_kwh": 10.0}},
        )

        self.assertEqual(advisor.status, "IMPROVING")
        self.assertIn("below last period", advisor.headline)
        self.assertLess(advisor.estimated_financial_impact.billing_period, 0)
        self.assertIn("Projected bill", [item.source for item in advisor.evidence_trail.items])
        self.assertIn("Timeline", [item.source for item in advisor.evidence_trail.items])

    def test_high_when_projected_well_above_previous_period(self):
        advisor = CostAdvisor().advise(
            current_summary=Summary(average_daily_net_cost=5.0),
            previous_summary=Summary(net_cost=100.0),
            settings=Settings(),
            recommendations=[],
            timeline=None,
            health={"status": "ok", "report_age_minutes": 4},
            comparison={"deltas": {"grid_import_kwh": 35.0, "solar_generated_kwh": -5.0}},
        )

        self.assertEqual(advisor.status, "HIGH")
        self.assertIn("above last period", advisor.headline)
        self.assertGreater(advisor.estimated_financial_impact.billing_period, 0)
        self.assertIn("Tariff contribution", [item.source for item in advisor.evidence_trail.items])

    def test_incomplete_billing_period_reduces_evidence_quality(self):
        advisor = CostAdvisor().advise(
            current_summary=Summary(average_daily_net_cost=5.0, supply_days=30, reading_days=12),
            previous_summary=Summary(net_cost=100.0),
            settings=Settings(),
            recommendations=[],
            timeline=None,
            health={"status": "ok", "report_age_minutes": 4},
            comparison={"deltas": {}},
        )

        self.assertEqual(advisor.evidence_summary.evidence_quality.value, "MEDIUM")
        self.assertEqual(advisor.confidence, "Medium")
        self.assertIn("Billing period is incomplete", advisor.evidence_summary.reason)

    def test_unknown_without_previous_period(self):
        advisor = CostAdvisor().advise(
            current_summary=Summary(),
            previous_summary=None,
            settings=Settings(),
            recommendations=[],
            timeline=None,
            health={"status": "ok", "report_age_minutes": 4},
            comparison=None,
        )

        self.assertEqual(advisor.status, "UNKNOWN")
        self.assertEqual(advisor.confidence, "Low")
        self.assertIn("cannot reliably compare", advisor.summary)


if __name__ == "__main__":
    unittest.main()
