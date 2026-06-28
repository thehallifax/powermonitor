import unittest
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from engine.charging_advisor import ChargingAdvisor
from engine.models import Advisor, AdvisorCollection, EvidenceTrail, FinancialImpact


LOCAL = ZoneInfo("Australia/Perth")


@dataclass
class Summary:
    intervals: int = 12
    solar_generated_kwh: float = 4.0
    solar_exported_kwh: float = 0.0
    solar_export_pct: float = 0.0
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


def advisor(status, impact=0.0, confidence="High", advisor_id="advisor"):
    return Advisor(
        advisor_id=advisor_id,
        advisor_type="TEST",
        status=status,
        headline=f"{status} headline",
        summary="Summary",
        recommendation="Recommendation",
        estimated_financial_impact=FinancialImpact(today=impact),
        confidence=confidence,
        confidence_reason="Because evidence is available.",
        evidence_trail=EvidenceTrail(),
        freshness="Fresh",
        supporting_data_sources=["Test data"],
        next_review_time=None,
    )


class AdvisorFrameworkTests(unittest.TestCase):
    def test_advisor_contract_serializes_shared_fields(self):
        item = advisor("CHARGE_NOW", impact=0.42, advisor_id="charging_advisor")

        data = item.as_dict()

        self.assertEqual(data["advisor_id"], "charging_advisor")
        self.assertEqual(data["estimated_financial_impact"]["today"], 0.42)
        self.assertEqual(data["evidence_trail"]["items"], [])

    def test_advisor_collection_orders_active_advisors_by_urgency_then_impact(self):
        collection = AdvisorCollection(
            [
                advisor("WAIT", impact=10.0, advisor_id="wait"),
                advisor("AVOID", impact=1.0, advisor_id="avoid"),
                advisor("CHARGE_NOW", impact=3.0, advisor_id="charge_small"),
                advisor("CHARGE_NOW", impact=8.0, advisor_id="charge_large"),
            ]
        )

        ordered = [item.advisor_id for item in collection.active()]

        self.assertEqual(ordered, ["avoid", "charge_large", "charge_small", "wait"])

    def test_charging_advisor_exposes_shared_advisor_without_changing_status(self):
        advice = ChargingAdvisor().advise(
            today_summary=Summary(),
            billing_summary=Summary(),
            settings=SETTINGS,
            health={"status": "ok", "report_age_minutes": 5},
            recommendations=[],
            projected_bill=88.0,
            now=datetime(2026, 6, 27, 12, tzinfo=LOCAL),
        )

        self.assertEqual(advice.status, advice.advisor.status)
        self.assertEqual(advice.headline, advice.advisor.headline)
        self.assertEqual(advice.explanation, advice.advisor.summary)
        self.assertEqual(advice.estimated_financial_benefit, advice.advisor.estimated_financial_impact)
        self.assertEqual(advice.evidence_trail, advice.advisor.evidence_trail)
        self.assertIn("Configured tariff schedule", advice.advisor.supporting_data_sources)


if __name__ == "__main__":
    unittest.main()
