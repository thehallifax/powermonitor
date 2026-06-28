import unittest
from datetime import date

from engine.briefing_engine import BriefingEngine
from engine.models import Advisor, AdvisorCollection, EvidenceTrail, FinancialImpact


def advisor(status, advisor_id, impact=0.0, confidence="High"):
    return Advisor(
        advisor_id=advisor_id,
        advisor_type="TEST",
        status=status,
        headline=f"{status} headline",
        summary=f"{status} summary",
        recommendation=f"{status} action",
        estimated_financial_impact=FinancialImpact(billing_period=impact),
        confidence=confidence,
        confidence_reason=f"{confidence} confidence reason",
        evidence_trail=EvidenceTrail(),
        freshness="Fresh",
        supporting_data_sources=["Test advisor"],
        next_review_time=None,
    )


class BriefingEngineTests(unittest.TestCase):
    def test_builds_daily_briefing_from_advisor_collection_only(self):
        briefing = BriefingEngine().build(
            AdvisorCollection(
                [
                    advisor("IMPROVING", "cost", -20.0),
                    advisor("CHARGE_NOW", "charging", 0.45),
                    advisor("WAIT", "watch", 0.1, "Medium"),
                ]
            ),
            date(2026, 6, 28),
        )

        self.assertIn("Sunday", briefing.greeting)
        self.assertEqual(briefing.top_priorities[0].advisor_id, "charging")
        self.assertEqual(briefing.positive_outcomes[0].advisor_id, "cost")
        self.assertEqual(briefing.items_to_watch[0].advisor_id, "watch")
        self.assertEqual(briefing.suggested_next_action, "CHARGE_NOW action")
        self.assertEqual(briefing.overall_confidence, "Medium")
        self.assertEqual(briefing.read_time_minutes, 1)

    def test_caps_briefing_sections(self):
        briefing = BriefingEngine().build(
            AdvisorCollection(
                [
                    advisor("HIGH", "high"),
                    advisor("CHARGE_NOW", "charge"),
                    advisor("SLIGHTLY_HIGH", "slightly"),
                    advisor("AVOID", "avoid"),
                    advisor("IMPROVING", "improving"),
                    advisor("ON_TRACK", "track"),
                    advisor("WAIT", "wait"),
                    advisor("UNKNOWN", "unknown"),
                    advisor("WAIT", "wait2"),
                ]
            )
        )

        self.assertLessEqual(len(briefing.top_priorities), 3)
        self.assertLessEqual(len(briefing.positive_outcomes), 2)
        self.assertLessEqual(len(briefing.items_to_watch), 2)

    def test_serializes_shared_briefing_model(self):
        briefing = BriefingEngine().build(AdvisorCollection([advisor("ON_TRACK", "cost")]))
        data = briefing.as_dict()

        self.assertEqual(data["positive_outcomes"][0]["advisor_id"], "cost")
        self.assertEqual(data["sections"][1]["section_type"], "positive_outcomes")
        self.assertIn("suggested_next_action", data)


if __name__ == "__main__":
    unittest.main()
