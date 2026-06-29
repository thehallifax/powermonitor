import unittest
from datetime import date
from datetime import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from engine.briefing_engine import BriefingEngine
from engine.context_engine import ContextEngine
from engine.models import Advisor, AdvisorCollection, EvidenceTrail, FinancialImpact


LOCAL = ZoneInfo("Australia/Perth")


@dataclass
class ContextSummary:
    intervals: int = 12
    reading_days: int = 8
    solar_generated_kwh: float = 8.0
    solar_used_kwh: float = 5.0
    solar_exported_kwh: float = 3.0
    solar_export_pct: float = 35.0
    ev_charging_source: str = "ocular_export"
    net_cost: float = 40.0


def household_context(now):
    summary = ContextSummary()
    return ContextEngine().build(
        today_summary=summary,
        billing_summary=summary,
        settings={"import_rates": {"super_off_peak": 0.08, "overnight": 0.19, "off_peak": 0.23, "peak": 0.53}},
        health={"status": "ok", "report_age_minutes": 5},
        now=now,
    )


def advisor(status, advisor_id, impact=0.0, confidence="High", advisor_type="TEST", recommendation=None, headline=None):
    return Advisor(
        advisor_id=advisor_id,
        advisor_type=advisor_type,
        status=status,
        headline=headline or f"{status} headline",
        summary=f"{status} summary",
        recommendation=recommendation or f"{status} action",
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

    def test_merges_related_ev_and_cost_actions_into_one_best_action(self):
        charging = advisor(
            "WAIT",
            "charging_advisor",
            impact=1.8,
            advisor_type="EV_CHARGING",
            recommendation="Charge your EV after 11 pm.",
            headline="Charging is cheaper after 11 pm.",
        )
        cost = advisor(
            "SLIGHTLY_HIGH",
            "cost_advisor",
            impact=12.0,
            advisor_type="COST",
            recommendation="Reduce charging costs before the bill closes.",
            headline="Your bill is slightly high.",
        )

        briefing = BriefingEngine().build(AdvisorCollection([charging, cost]))

        self.assertEqual(len(briefing.top_priorities), 1)
        self.assertEqual(briefing.top_priorities[0].recommendation, "Reduce charging costs before the bill closes.")
        self.assertEqual(
            briefing.top_priorities[0].supporting_advisors,
            ["Cost", "Ev Charging"],
        )
        self.assertEqual(len(briefing.advisor_summary), 2)

    def test_merges_solar_and_efficiency_into_one_household_action(self):
        solar = advisor(
            "MISSED_OPPORTUNITY",
            "solar_advisor",
            impact=9.0,
            advisor_type="SOLAR",
            recommendation="Review active solar recommendations and consider moving flexible loads into sunny periods.",
        )
        efficiency = advisor(
            "INEFFICIENT",
            "efficiency_advisor",
            impact=7.5,
            advisor_type="EFFICIENCY",
            recommendation="Review daytime imports and shift flexible usage where practical.",
        )

        briefing = BriefingEngine().build(AdvisorCollection([solar, efficiency]))

        self.assertEqual(len(briefing.top_priorities), 1)
        self.assertEqual(briefing.top_priorities[0].title, "Shift flexible loads into your solar window.")
        self.assertEqual(
            briefing.top_priorities[0].supporting_advisors,
            ["Efficiency", "Solar"],
        )

    def test_orchestration_emits_one_best_action_one_win_and_one_watch(self):
        briefing = BriefingEngine().build(
            AdvisorCollection(
                [
                    advisor("HIGH", "high"),
                    advisor("CHARGE_NOW", "charge"),
                    advisor("ON_TRACK", "cost", advisor_type="COST"),
                    advisor("GOOD", "solar", advisor_type="SOLAR"),
                    advisor("WATCH", "efficiency", advisor_type="EFFICIENCY"),
                    advisor("UNKNOWN", "unknown"),
                ]
            )
        )

        self.assertEqual(len(briefing.top_priorities), 1)
        self.assertEqual(len(briefing.positive_outcomes), 1)
        self.assertEqual(len(briefing.items_to_watch), 1)

    def test_night_context_briefing_focuses_on_overnight_and_tomorrow(self):
        briefing = BriefingEngine().build(
            AdvisorCollection([advisor("MISSED_OPPORTUNITY", "solar", advisor_type="SOLAR")]),
            date(2026, 6, 28),
            context=household_context(datetime(2026, 6, 28, 23, 30, tzinfo=LOCAL)),
        )

        self.assertIn("Night context", briefing.overall_household_status)
        self.assertIn("tomorrow", briefing.suggested_next_action.lower())

    def test_day_context_briefing_keeps_solar_focus(self):
        briefing = BriefingEngine().build(
            AdvisorCollection([advisor("MISSED_OPPORTUNITY", "solar", advisor_type="SOLAR")]),
            date(2026, 6, 28),
            context=household_context(datetime(2026, 6, 28, 12, 0, tzinfo=LOCAL)),
        )

        self.assertIn("Daylight context", briefing.overall_household_status)
        self.assertIn("solar", briefing.overall_household_status.lower())


if __name__ == "__main__":
    unittest.main()
