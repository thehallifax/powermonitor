import unittest
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from engine.charging_advisor import ChargingAdvisor, current_tariff_bucket
from engine.models import Decision, FinancialImpact


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


def decision(stable_id="peak_cost_reduction", priority_score=72):
    return Decision(
        id=stable_id,
        title="Reduce Peak imports",
        summary="Peak import is high.",
        finding="Peak import is high.",
        evidence={"priority_score": priority_score},
        financial_impact=FinancialImpact(billing_period=4.2),
        recommendation="Move flexible load into a cheaper window.",
        confidence="Medium",
        priority="High",
        expires_at=None,
        data_sources_used=["SolarEdge interval data"],
        category="Tariffs",
        action_type="load_shift",
    )


class ChargingAdvisorTests(unittest.TestCase):
    def test_super_off_peak_recommends_charge_now_with_measurable_benefit(self):
        advice = ChargingAdvisor().advise(
            today_summary=Summary(),
            billing_summary=Summary(),
            settings=SETTINGS,
            health={"status": "ok", "report_age_minutes": 5},
            recommendations=[decision()],
            projected_bill=88.0,
            now=datetime(2026, 6, 27, 12, tzinfo=LOCAL),
        )

        self.assertEqual(advice.status, "CHARGE_NOW")
        self.assertEqual(advice.confidence, "High")
        self.assertIsNotNone(advice.estimated_financial_benefit)
        self.assertEqual(advice.supporting_evidence["current_tariff_bucket"], "super_off_peak")
        self.assertEqual(advice.current_window, "9am-3pm super off-peak / EV solar window")
        self.assertEqual(advice.current_window_label, "Super off peak")
        self.assertEqual(advice.current_window_rate, SETTINGS.import_rates["super_off_peak"])
        self.assertEqual(advice.tariff_now_label, "Super off peak")
        self.assertEqual(advice.tariff_next_label, "Super off peak")
        self.assertEqual(advice.next_lower_cost_window_label, "Super off peak")
        self.assertEqual(advice.lowest_cost_window_label, "Super off peak")
        self.assertEqual(advice.recommended_wait_minutes, 0)
        self.assertEqual(advice.per_kwh_difference, 0.0)
        self.assertIn("tariff timing", advice.confidence_reason)

    def test_peak_recommends_avoid_and_explains_bill_pressure(self):
        advice = ChargingAdvisor().advise(
            today_summary=Summary(),
            billing_summary=Summary(),
            settings=SETTINGS,
            health={"status": "ok", "report_age_minutes": 5},
            recommendations=[decision("high_cost_alert", 80)],
            projected_bill=220.0,
            now=datetime(2026, 6, 27, 18, tzinfo=LOCAL),
        )

        self.assertEqual(advice.status, "AVOID")
        self.assertIn("tariff-based", advice.explanation)
        self.assertIn("billing position", advice.explanation)
        self.assertGreater(advice.estimated_financial_benefit.today, 0)
        self.assertEqual(advice.next_lower_cost_window, "Off peak from 9:00 PM")
        self.assertTrue(advice.next_lower_cost_window_start.endswith("21:00:00+08:00"))
        self.assertEqual(advice.next_lower_cost_window_label, "Off peak")
        self.assertEqual(advice.next_window, advice.next_lower_cost_window)
        self.assertEqual(advice.tariff_next_label, "Off peak")
        self.assertEqual(advice.lowest_cost_window, "Super off peak from 9:00 AM")
        self.assertTrue(advice.lowest_cost_window_start.endswith("09:00:00+08:00"))
        self.assertEqual(advice.lowest_cost_window_label, "Super off peak")
        self.assertEqual(advice.recommended_wait_minutes, 180)
        self.assertAlmostEqual(advice.per_kwh_difference, 0.3015)
        self.assertIn("next cheaper window", advice.explanation)
        self.assertIn("lowest-cost window", advice.explanation)

    def test_multistep_tariff_separates_next_lower_from_lowest_cost(self):
        settings = Settings(
            import_rates={
                "super_off_peak": 0.25,
                "overnight": 0.10,
                "off_peak": 0.30,
                "peak": 0.55,
            }
        )

        advice = ChargingAdvisor().advise(
            today_summary=Summary(),
            billing_summary=Summary(),
            settings=settings,
            health={"status": "ok", "report_age_minutes": 5},
            recommendations=[],
            projected_bill=220.0,
            now=datetime(2026, 6, 27, 18, tzinfo=LOCAL),
        )

        self.assertEqual(advice.next_lower_cost_window, "Off peak from 9:00 PM")
        self.assertEqual(advice.lowest_cost_window, "Overnight from 11:00 PM")
        self.assertEqual(advice.next_lower_cost_window_rate, 0.30)
        self.assertEqual(advice.lowest_cost_window_rate, 0.10)
        self.assertEqual(advice.recommended_wait_minutes, 180)

    def test_solar_export_alone_waits_outside_super_off_peak(self):
        advice = ChargingAdvisor().advise(
            today_summary=Summary(solar_exported_kwh=1.2, solar_export_pct=30.0),
            billing_summary=Summary(),
            settings=SETTINGS,
            health={"status": "ok", "report_age_minutes": 5},
            recommendations=[],
            projected_bill=80.0,
            now=datetime(2026, 6, 27, 8, tzinfo=LOCAL),
        )

        self.assertEqual(current_tariff_bucket(datetime(2026, 6, 27, 8, tzinfo=LOCAL)), "off_peak")
        self.assertEqual(advice.status, "WAIT")
        self.assertIn("cannot prove surplus solar exists right now", advice.explanation)
        self.assertEqual(advice.confidence, "Low")
        self.assertEqual(advice.advisor.evidence_summary.evidence_quality.value, "LOW")
        self.assertIn("Today's solar summary", advice.solar_data_status)

    def test_stale_data_returns_unknown(self):
        advice = ChargingAdvisor().advise(
            today_summary=Summary(),
            billing_summary=Summary(),
            settings=SETTINGS,
            health={"status": "ok", "report_age_minutes": 240},
            recommendations=[],
            projected_bill=80.0,
            now=datetime(2026, 6, 27, 12, tzinfo=LOCAL),
        )

        self.assertEqual(advice.status, "UNKNOWN")
        self.assertEqual(advice.confidence, "Low")
        self.assertEqual(advice.data_freshness, "Report age 240 minutes")
        self.assertIn("Low confidence", advice.confidence_reason)

    def test_medium_confidence_explains_freshness_limit(self):
        advice = ChargingAdvisor().advise(
            today_summary=Summary(ev_charging_source="estimated"),
            billing_summary=Summary(ev_charging_source="estimated"),
            settings=SETTINGS,
            health={"status": "ok", "report_age_minutes": 90},
            recommendations=[],
            projected_bill=80.0,
            now=datetime(2026, 6, 27, 21, 30, tzinfo=LOCAL),
        )

        self.assertEqual(advice.status, "WAIT")
        self.assertEqual(advice.confidence, "Medium")
        self.assertEqual(advice.charger_data_status, "estimated_fallback")
        self.assertIn("report freshness", advice.confidence_reason)
        self.assertEqual(advice.next_window, "Overnight from 11:00 PM")
        self.assertEqual(advice.lowest_cost_window, "Super off peak from 9:00 AM")

    def test_charging_advice_serializes_refined_tariff_windows(self):
        advice = ChargingAdvisor().advise(
            today_summary=Summary(),
            billing_summary=Summary(),
            settings=SETTINGS,
            health={"status": "ok", "report_age_minutes": 5},
            recommendations=[],
            projected_bill=80.0,
            now=datetime(2026, 6, 27, 18, tzinfo=LOCAL),
        )

        data = advice.as_dict()

        self.assertEqual(data["next_lower_cost_window"], "Off peak from 9:00 PM")
        self.assertEqual(data["lowest_cost_window"], "Super off peak from 9:00 AM")
        self.assertEqual(data["recommended_wait_minutes"], 180)


if __name__ == "__main__":
    unittest.main()
