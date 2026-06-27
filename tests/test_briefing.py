import unittest
from datetime import date

from engine.models import Decision, FinancialImpact
from presentation.briefing import build_daily_briefing


def decision(
    decision_id: str,
    title: str,
    priority: str,
    action_type: str,
    billing_period_impact=None,
) -> Decision:
    return Decision(
        id=decision_id,
        title=title,
        summary=f"{title} summary",
        finding=f"{title} finding.",
        evidence={},
        financial_impact=FinancialImpact(billing_period=billing_period_impact),
        recommendation=f"{title} recommendation.",
        confidence="High" if action_type == "maintain" else "Medium",
        priority=priority,
        expires_at=None,
        data_sources_used=["SolarEdge interval data", "Tariff settings"],
        category="General",
        action_type=action_type,
    )


class DailyBriefingTests(unittest.TestCase):
    def test_builds_priority_win_and_opportunities_from_decisions(self):
        briefing = build_daily_briefing(
            [
                decision("positive_reinforcement", "You're doing well", "Low", "maintain"),
                decision("solar_self_consumption", "Use more solar at home", "Medium", "load_shift"),
                decision("high_cost_alert", "Projected billing cost is high", "High", "review", 310.25),
                decision("baseload_monitoring", "Review overnight baseload", "Medium", "investigate"),
                decision("ev_charging_optimisation", "Actual EV charging", "Low", "tariff_window"),
            ],
            date(2026, 6, 27),
        )

        self.assertEqual(briefing.todays_priority.decision_id, "high_cost_alert")
        self.assertEqual(briefing.todays_win.decision_id, "positive_reinforcement")
        self.assertEqual(
            [item.decision_id for item in briefing.opportunities],
            ["baseload_monitoring", "solar_self_consumption", "ev_charging_optimisation"],
        )
        self.assertEqual(briefing.todays_priority.financial_impact, "$310.25 this billing period")
        self.assertEqual(briefing.read_time_minutes, 1)
        self.assertEqual(
            briefing.data_sources_used,
            ["SolarEdge interval data", "Tariff settings"],
        )

    def test_empty_decisions_produce_no_action_briefing(self):
        briefing = build_daily_briefing([], date(2026, 6, 27))

        self.assertIsNone(briefing.todays_priority)
        self.assertIsNone(briefing.todays_win)
        self.assertEqual(briefing.opportunities, [])
        self.assertEqual(briefing.confidence, "Unknown")


if __name__ == "__main__":
    unittest.main()
