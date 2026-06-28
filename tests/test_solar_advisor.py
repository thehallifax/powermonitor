import unittest
from dataclasses import dataclass

from engine.models import DecisionTimeline, TimelineEntry
from engine.solar_advisor import SolarAdvisor


@dataclass
class Summary:
    supply_days: int = 20
    reading_days: int = 18
    intervals: int = 100
    solar_generated_kwh: float = 40.0
    solar_used_kwh: float = 30.0
    solar_exported_kwh: float = 10.0
    self_consumption_pct: float = 75.0
    solar_export_pct: float = 25.0
    grid_import_kwh: float = 100.0
    import_cost: float = 30.0


@dataclass
class Settings:
    export_credit_rate: float = 0.02


def timeline():
    return DecisionTimeline(
        [
            TimelineEntry(
                timestamp="2026-06-20T12:00:00+08:00",
                title="Solar reviewed",
                summary="Solar utilisation was reviewed.",
                financial_impact=None,
                confidence="High",
                evidence_sources=["SolarEdge interval data"],
                event_type="solar_review",
            )
        ]
    )


class SolarAdvisorTests(unittest.TestCase):
    def test_excellent_when_self_consumption_is_high(self):
        advisor = SolarAdvisor().advise(
            summary=Summary(),
            settings=Settings(),
            recommendations=[],
            timeline=timeline(),
            health={"status": "ok", "report_age_minutes": 4},
        )

        self.assertEqual(advisor.status, "EXCELLENT")
        self.assertIn("excellent", advisor.headline)
        self.assertIn("75.0%", advisor.summary)
        self.assertIn("Timeline", [item.source for item in advisor.evidence_trail.items])

    def test_missed_opportunity_when_exports_are_high(self):
        advisor = SolarAdvisor().advise(
            summary=Summary(
                solar_generated_kwh=25.0,
                solar_used_kwh=10.0,
                solar_exported_kwh=15.0,
                self_consumption_pct=40.0,
                solar_export_pct=60.0,
            ),
            settings=Settings(),
            recommendations=[],
            timeline=None,
            health={"status": "ok", "report_age_minutes": 4},
        )

        self.assertEqual(advisor.status, "MISSED_OPPORTUNITY")
        self.assertIn("15.0 kWh", advisor.headline)
        self.assertGreater(advisor.estimated_financial_impact.billing_period, 0)
        sources = [item.source for item in advisor.evidence_trail.items]
        self.assertIn("Exported energy", sources)
        self.assertIn("Import offset", sources)
        self.assertIn("Billing contribution", sources)

    def test_unknown_without_solar_data(self):
        advisor = SolarAdvisor().advise(
            summary=Summary(
                intervals=0,
                solar_generated_kwh=0.0,
                solar_used_kwh=0.0,
                solar_exported_kwh=0.0,
                self_consumption_pct=0.0,
                solar_export_pct=0.0,
            ),
            settings=Settings(),
            recommendations=[],
            timeline=None,
            health={"status": "ok", "report_age_minutes": 4},
        )

        self.assertEqual(advisor.status, "UNKNOWN")
        self.assertEqual(advisor.confidence, "Low")
        self.assertIsNone(advisor.estimated_financial_impact)


if __name__ == "__main__":
    unittest.main()
