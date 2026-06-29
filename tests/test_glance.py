import unittest
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from engine.charging_advisor import ChargingAdvisor
from engine.models import Decision, EnergyScore, FinancialImpact
from presentation.glance import build_glance_state, current_tariff_bucket


LOCAL = ZoneInfo("Australia/Perth")


@dataclass
class Summary:
    intervals: int = 10
    solar_generated_kwh: float = 3.0
    solar_exported_kwh: float = 0.0
    solar_export_pct: float = 0.0
    net_cost: float = 12.5
    ev_charging_kwh: float = 8.0
    ev_charging_source: str = "ocular_export"


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


def decision() -> Decision:
    return Decision(
        id="peak_cost_reduction",
        title="Reduce Peak imports",
        summary="Peak import is high.",
        finding="Peak import is high.",
        evidence={},
        financial_impact=FinancialImpact(billing_period=4.2),
        recommendation="Move flexible load into a cheaper window.",
        confidence="Medium",
        priority="Medium",
        expires_at=None,
        data_sources_used=["SolarEdge interval data"],
        category="Tariffs",
        action_type="load_shift",
    )


def energy_score() -> EnergyScore:
    return EnergyScore(
        score=84,
        label="Good",
        summary="Good energy performance.",
        top_positive_factors=[],
        top_negative_factors=[],
        potential_score=90,
        confidence="High",
        data_sources_used=["SolarEdge interval data"],
        explanation=None,
    )


def build_state(now, today=None, health=None):
    today_summary = today or Summary()
    billing_summary = Summary(net_cost=41.25)
    advice = ChargingAdvisor().advise(
        today_summary=today_summary,
        billing_summary=billing_summary,
        settings=SETTINGS,
        health=health or {"status": "ok", "report_age_minutes": 5},
        recommendations=[decision()],
        projected_bill=82.5,
        now=now,
    )
    return build_glance_state(
        billing_summary=billing_summary,
        charging_advice=advice,
        energy_score=energy_score(),
        projected_bill=82.5,
        bill_projection_basis="configured billing end",
        now=now,
    )


class GlanceModeTests(unittest.TestCase):
    def test_current_tariff_bucket_uses_existing_synergy_windows(self):
        self.assertEqual(current_tariff_bucket(datetime(2026, 6, 27, 2, tzinfo=LOCAL)), "overnight")
        self.assertEqual(current_tariff_bucket(datetime(2026, 6, 27, 8, tzinfo=LOCAL)), "off_peak")
        self.assertEqual(current_tariff_bucket(datetime(2026, 6, 27, 12, tzinfo=LOCAL)), "super_off_peak")
        self.assertEqual(current_tariff_bucket(datetime(2026, 6, 27, 17, tzinfo=LOCAL)), "peak")
        self.assertEqual(current_tariff_bucket(datetime(2026, 6, 27, 22, tzinfo=LOCAL)), "off_peak")

    def test_super_off_peak_maps_to_charge_now(self):
        state = build_state(datetime(2026, 6, 27, 12, 0, tzinfo=LOCAL))

        self.assertEqual(state.status, "CHARGE NOW")
        self.assertEqual(state.reason, "Use this window for charging or flexible loads.")
        self.assertEqual(state.current_tariff, "Super off peak")
        self.assertEqual(state.energy_score, 84)
        self.assertEqual(state.bill_current, 41.25)
        self.assertEqual(state.bill_projected, 82.5)

    def test_export_only_solar_maps_to_wait_outside_super_off_peak(self):
        state = build_state(
            datetime(2026, 6, 27, 15, 30, tzinfo=LOCAL),
            today=Summary(solar_generated_kwh=5.0, solar_exported_kwh=1.4, solar_export_pct=28.0),
        )

        self.assertEqual(state.status, "WAIT")
        self.assertEqual(state.reason, "Wait 330 minutes for cheaper charging.")
        self.assertEqual(state.evidence_quality, "LOW")
        self.assertIn("Exporting solar", state.solar_status)

    def test_peak_maps_to_avoid(self):
        state = build_state(datetime(2026, 6, 27, 18, 0, tzinfo=LOCAL))

        self.assertEqual(state.status, "AVOID")
        self.assertEqual(state.reason, "Avoid charging now. Cheaper charging starts in 180 minutes.")
        self.assertEqual(state.current_recommendation, "Use Off peak from 9:00 PM instead.")
        self.assertEqual(state.next_window, "Off peak from 9:00 PM")
        self.assertEqual(state.lowest_cost_window, "Super off peak from 9:00 AM")

    def test_off_peak_without_surplus_maps_to_wait(self):
        state = build_state(datetime(2026, 6, 27, 21, 30, tzinfo=LOCAL))

        self.assertEqual(state.status, "WAIT")
        self.assertEqual(state.reason, "Wait 90 minutes for cheaper charging.")
        self.assertIn("Overnight", state.next_window)
        self.assertIn("Super off peak", state.lowest_cost_window)

    def test_stale_or_missing_data_maps_to_unavailable(self):
        state = build_state(
            datetime(2026, 6, 27, 12, 0, tzinfo=LOCAL),
            health={"status": "stale", "report_age_minutes": 5},
        )

        self.assertEqual(state.status, "DATA UNAVAILABLE")
        self.assertEqual(state.reason, "Charging advice needs fresher data.")
        self.assertEqual(state.confidence, "Low")
        self.assertEqual(state.evidence_quality, "LOW")


if __name__ == "__main__":
    unittest.main()
