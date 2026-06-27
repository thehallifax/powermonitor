import unittest
from dataclasses import dataclass
from datetime import date

from engine.decision_engine import generate_decisions, generate_decisions_from_data_engine
from engine.models import DecisionTimeline, FinancialImpact, TimelineEntry


@dataclass
class Settings:
    current_billing_period_start: date = date(2026, 6, 6)
    current_billing_period_end = None
    previous_billing_period_start = None
    previous_billing_period_end = None
    import_rates = {
        "super_off_peak": 0.08,
        "overnight": 0.19,
        "off_peak": 0.23,
        "peak": 0.54,
    }
    ev_baseline_watts = 250.0
    recommendation_thresholds = {
        "peak_cost_share_pct": 25.0,
        "peak_shift_kwh_per_day": 5.0,
        "solar_self_consumption_pct": 60.0,
        "ev_detected_min_kwh": 0.5,
        "baseload_excess_pct": 20.0,
        "high_cost_threshold": 300.0,
        "positive_peak_share_pct": 20.0,
        "positive_self_consumption_pct": 70.0,
        "trend_repeated_event_count": 2.0,
    }


class TimelineDataEngine:
    def __init__(self, timeline):
        self.timeline = timeline
        self.timeline_requested = False

    def get_overnight_baseload_watts(self, start, end):
        return 0.0

    def get_timeline_entries(self, limit=100):
        self.timeline_requested = True
        return self.timeline


def timeline_entry(event_type, title, sources=None, impact=None):
    return TimelineEntry(
        timestamp="2026-06-27",
        title=title,
        summary=f"{title} summary.",
        financial_impact=impact,
        confidence="Medium",
        evidence_sources=sources or ["PowerMonitor timeline history"],
        event_type=event_type,
    )


def context_with(entries):
    return {
        "current": {
            "start": date(2026, 6, 6),
            "end": date(2026, 6, 27),
            "supply_days": 22,
            "reading_days": 22,
            "peak_cost_share_pct": 0.0,
            "buckets": {"peak": {"kwh": 0.0, "cost": 0.0}},
            "self_consumption_pct": 65.0,
            "solar_export_pct": 35.0,
            "ev_charging_kwh": 0.0,
            "ev_charging_cost": 0.0,
            "ev_charging_source": "ocular_export",
            "net_cost": 80.0,
            "average_daily_net_cost": 3.6,
        },
        "previous": None,
        "import_rates": Settings.import_rates,
        "ev_data_source": "ocular_export",
        "ev_baseline_watts": 250.0,
        "overnight_baseload_watts": 0.0,
        "timeline_history": DecisionTimeline(entries=entries),
        "projected_period_days": 22,
        "projected_period_end": "2026-06-27",
        "projected_net_cost": 80.0,
        "projected_bill_impact": FinancialImpact(billing_period=80.0),
    }


class TrendAwareDecisionTests(unittest.TestCase):
    def ids_for_entries(self, entries):
        decisions = generate_decisions(context_with(entries), Settings.recommendation_thresholds)
        return {decision.id: decision for decision in decisions}

    def test_repeated_high_peak_usage_generates_trend_decision(self):
        decisions = self.ids_for_entries([
            timeline_entry("high_peak_usage", "High peak usage", impact="$5.00 this billing period"),
            timeline_entry("high_peak_usage", "High peak usage", impact="$4.00 this billing period"),
        ])

        decision = decisions["trend_repeated_peak_usage"]
        self.assertEqual(decision.evidence["high_peak_usage_events"], 2)
        self.assertIn("PowerMonitor timeline history", decision.data_sources_used)
        self.assertIsNotNone(decision.explanation)

    def test_bill_trajectory_generates_improving_or_worsening_decision(self):
        improving = self.ids_for_entries([
            timeline_entry("bill_tracking", "Bill tracking improved"),
            timeline_entry("bill_tracking", "Bill tracking improved"),
            timeline_entry("bill_tracking", "Bill tracking worsened"),
        ])
        worsening = self.ids_for_entries([
            timeline_entry("bill_tracking", "Bill tracking worsened"),
            timeline_entry("bill_tracking", "Bill tracking worsened"),
        ])

        self.assertIn("trend_bill_trajectory_improving", improving)
        self.assertIn("trend_bill_trajectory_worsening", worsening)

    def test_recurring_ev_and_solar_wins_generate_maintain_decisions(self):
        decisions = self.ids_for_entries([
            timeline_entry("ev_cheapest_tariff", "EV charged in cheapest tariff"),
            timeline_entry("ev_cheapest_tariff", "EV charged in cheapest tariff"),
            timeline_entry("positive_outcome", "Excellent utilisation signal"),
            timeline_entry("positive_outcome", "Excellent utilisation signal"),
        ])

        self.assertIn("trend_recurring_ev_charging_win", decisions)
        self.assertIn("trend_recurring_solar_utilisation_win", decisions)
        self.assertEqual(decisions["trend_recurring_ev_charging_win"].action_type, "maintain")

    def test_repeated_estimated_data_warning_uses_timeline_sources(self):
        decisions = self.ids_for_entries([
            timeline_entry("ev_charging_reviewed", "EV charging reviewed", ["Estimated EV charging"]),
            timeline_entry("decision_data_quality", "Estimated data is recurring", ["Estimated EV charging"]),
        ])

        decision = decisions["trend_repeated_estimated_data_warning"]
        self.assertEqual(decision.evidence["estimated_ev_timeline_events"], 2)
        self.assertIn("Estimated EV charging", decision.data_sources_used)

    def test_data_engine_timeline_history_feeds_decision_context(self):
        engine = TimelineDataEngine(
            DecisionTimeline(entries=[
                timeline_entry("high_peak_usage", "High peak usage"),
                timeline_entry("high_peak_usage", "High peak usage"),
            ])
        )
        summary = context_with([])["current"]

        decisions = generate_decisions_from_data_engine(
            summary,
            Settings(),
            engine,
            Settings.recommendation_thresholds,
        )

        self.assertTrue(engine.timeline_requested)
        self.assertIn("trend_repeated_peak_usage", [decision.id for decision in decisions])


if __name__ == "__main__":
    unittest.main()
