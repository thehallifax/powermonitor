import unittest
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from engine.context_engine import ContextEngine
from engine.models import DecisionTimeline, TimelineEntry
from engine.solar_advisor import SolarAdvisor


LOCAL = ZoneInfo("Australia/Perth")


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
    import_rates: dict = None


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
    def context(self, now, summary):
        return ContextEngine().build(
            today_summary=summary,
            billing_summary=summary,
            settings={"import_rates": {"super_off_peak": 0.08, "overnight": 0.19, "off_peak": 0.23, "peak": 0.53}},
            health={"status": "ok", "report_age_minutes": 4},
            now=now,
        )

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
        self.assertEqual(advisor.evidence_summary.evidence_quality.value, "HIGH")

    def test_missed_opportunity_when_exports_are_high(self):
        summary = Summary(
            solar_generated_kwh=25.0,
            solar_used_kwh=10.0,
            solar_exported_kwh=15.0,
            self_consumption_pct=40.0,
            solar_export_pct=60.0,
        )
        advisor = SolarAdvisor().advise(
            summary=summary,
            settings=Settings(),
            recommendations=[],
            timeline=None,
            health={"status": "ok", "report_age_minutes": 4},
            context=self.context(datetime(2026, 6, 27, 12, tzinfo=LOCAL), summary),
        )

        self.assertEqual(advisor.status, "MISSED_OPPORTUNITY")
        self.assertIn("15.0 kWh", advisor.headline)
        self.assertGreater(advisor.estimated_financial_impact.billing_period, 0)
        sources = [item.source for item in advisor.evidence_trail.items]
        self.assertIn("Exported energy", sources)
        self.assertIn("Import offset", sources)
        self.assertIn("Billing contribution", sources)

    def test_night_context_avoids_immediate_solar_opportunity_wording(self):
        summary = Summary(
            solar_generated_kwh=25.0,
            solar_used_kwh=10.0,
            solar_exported_kwh=15.0,
            self_consumption_pct=40.0,
            solar_export_pct=60.0,
        )

        advisor = SolarAdvisor().advise(
            summary=summary,
            settings=Settings(),
            recommendations=[],
            timeline=None,
            health={"status": "ok", "report_age_minutes": 4},
            context=self.context(datetime(2026, 6, 27, 23, 30, tzinfo=LOCAL), summary),
        )

        self.assertEqual(advisor.status, "MISSED_OPPORTUNITY")
        self.assertNotIn("could potentially have powered flexible loads", advisor.headline)
        self.assertIn("performance", advisor.summary)
        self.assertIn("next daylight window", advisor.recommendation)
        self.assertIn("Household context", [item.source for item in advisor.evidence_trail.items])

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
        self.assertEqual(advisor.evidence_summary.evidence_quality.value, "LOW")
        self.assertIn("Missing inverter/provider solar data", advisor.evidence_summary.reason)


if __name__ == "__main__":
    unittest.main()
