import unittest
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from engine.charging_advisor import ChargingAdvisor
from engine.models import EvidenceItem, EvidenceTrail, FinancialImpact

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report


LOCAL = ZoneInfo("Australia/Perth")


@dataclass
class Summary:
    intervals: int = 12
    solar_generated_kwh: float = 4.0
    solar_exported_kwh: float = 1.0
    solar_export_pct: float = 25.0
    ev_charging_kwh: float = 10.0
    ev_charging_source: str = "ocular_export"
    net_cost: float = 45.0


@dataclass
class Settings:
    import_rates: dict


SETTINGS = Settings(
    import_rates={
        "super_off_peak": 0.086151,
        "overnight": 0.193841,
        "off_peak": 0.236916,
        "peak": 0.538446,
    }
)


class EvidenceTrailTests(unittest.TestCase):
    def test_evidence_trail_serializes_ordered_items(self):
        trail = EvidenceTrail(
            items=[
                EvidenceItem(
                    source="Current tariff",
                    observation="Super Off Peak",
                    impact="Charging now is cheap.",
                    confidence="High",
                    freshness="Configured tariff schedule",
                    financial_contribution=FinancialImpact(today=0.12, basis="per kWh"),
                )
            ]
        )

        data = trail.as_dict()

        self.assertEqual(data["items"][0]["source"], "Current tariff")
        self.assertEqual(data["items"][0]["financial_contribution"]["today"], 0.12)

    def test_charging_advice_exposes_ordered_evidence_trail(self):
        advice = ChargingAdvisor().advise(
            today_summary=Summary(),
            billing_summary=Summary(),
            settings=SETTINGS,
            health={"status": "ok", "report_age_minutes": 4},
            recommendations=[],
            projected_bill=88.0,
            now=datetime(2026, 6, 27, 12, tzinfo=LOCAL),
        )

        sources = [item.source for item in advice.evidence_trail.items]

        self.assertEqual(sources[:4], ["Current tariff", "Next tariff", "Solar data", "EV sessions"])
        self.assertEqual(sources[-1], "Recommendation")
        self.assertEqual(advice.evidence_trail.items[-1].observation, advice.headline)

    def test_home_card_renders_expandable_reasoning(self):
        advice = ChargingAdvisor().advise(
            today_summary=Summary(),
            billing_summary=Summary(),
            settings=SETTINGS,
            health={"status": "ok", "report_age_minutes": 4},
            recommendations=[],
            projected_bill=88.0,
            now=datetime(2026, 6, 27, 12, tzinfo=LOCAL),
        )

        markup = report.charging_advice_home_html(advice)

        self.assertIn('<details class="evidence-trail">', markup)
        self.assertIn("<summary>Why?</summary>", markup)
        self.assertIn("Current tariff", markup)
        self.assertIn("Recommendation", markup)
        self.assertIn("Evidence", markup)

    def test_export_only_solar_does_not_trigger_charge_now(self):
        advice = ChargingAdvisor().advise(
            today_summary=Summary(solar_generated_kwh=8.0, solar_exported_kwh=5.3, solar_export_pct=40.0),
            billing_summary=Summary(),
            settings=SETTINGS,
            health={"status": "ok", "report_age_minutes": 4},
            recommendations=[],
            projected_bill=88.0,
            now=datetime(2026, 6, 27, 18, tzinfo=LOCAL),
        )

        self.assertEqual(advice.status, "WAIT")
        self.assertEqual(advice.confidence, "Low")
        self.assertEqual(advice.advisor.evidence_summary.evidence_quality.value, "LOW")
        self.assertIn("Live solar unavailable", advice.advisor.evidence_summary.reason)
        self.assertIn("cannot prove surplus solar exists right now", advice.explanation)

    def test_charging_advisor_serializes_evidence_quality_fields(self):
        advice = ChargingAdvisor().advise(
            today_summary=Summary(),
            billing_summary=Summary(),
            settings=SETTINGS,
            health={"status": "ok", "report_age_minutes": 4},
            recommendations=[],
            projected_bill=88.0,
            now=datetime(2026, 6, 27, 12, tzinfo=LOCAL),
        )

        advisor = advice.as_dict()["advisor"]

        self.assertIn("evidence_quality", advisor)
        self.assertIn("evidence_summary", advisor)
        self.assertIn("required_evidence", advisor)
        self.assertIn("available_evidence", advisor)
        self.assertIn("missing_evidence", advisor)
        self.assertEqual(advisor["evidence_quality"], "HIGH")


if __name__ == "__main__":
    unittest.main()
