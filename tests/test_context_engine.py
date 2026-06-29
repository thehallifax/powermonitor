import unittest
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from engine.context_engine import (
    BillingContext,
    ChargingContext,
    ContextConfidence,
    ContextEngine,
    EvidenceContext,
    SolarContext,
    TariffContext,
    TimeContext,
)


LOCAL = ZoneInfo("Australia/Perth")


@dataclass
class Summary:
    intervals: int = 12
    reading_days: int = 8
    solar_generated_kwh: float = 8.0
    solar_exported_kwh: float = 0.2
    solar_export_pct: float = 3.0
    ev_charging_source: str = "ocular_export"
    net_cost: float = 80.0


@dataclass
class Settings:
    import_rates: dict
    current_billing_period_end: Optional[date] = None
    recommendation_thresholds: Optional[dict] = None


RATES = {
    "super_off_peak": 0.086151,
    "overnight": 0.193841,
    "off_peak": 0.236916,
    "peak": 0.538446,
}


class ContextEngineTests(unittest.TestCase):
    def build(self, *, now: datetime, today_summary=None, billing_summary=None, settings=None, health=None):
        return ContextEngine().build(
            today_summary=today_summary or Summary(),
            billing_summary=billing_summary or Summary(),
            settings=settings or Settings(RATES),
            health=health or {"status": "ok", "report_age_minutes": 5},
            now=now,
        )

    def test_daytime_context_describes_solar_and_lowest_tariff(self):
        context = self.build(
            now=datetime(2026, 6, 27, 12, tzinfo=LOCAL),
            today_summary=Summary(solar_exported_kwh=1.4, solar_export_pct=25.0),
        )

        self.assertEqual(context.time, TimeContext.MIDDAY)
        self.assertEqual(context.solar, SolarContext.SOLAR_SURPLUS_POSSIBLE)
        self.assertEqual(context.tariff, TariffContext.LOWEST_COST)
        self.assertEqual(context.charging, ChargingContext.CHEAP_WINDOW_ACTIVE)
        self.assertEqual(context.evidence, EvidenceContext.LIVE)
        self.assertEqual(context.confidence, ContextConfidence.HIGH)

    def test_nighttime_context_does_not_guess_solar(self):
        context = self.build(now=datetime(2026, 6, 27, 2, tzinfo=LOCAL))

        self.assertEqual(context.time, TimeContext.NIGHT)
        self.assertEqual(context.solar, SolarContext.SOLAR_IMPOSSIBLE)
        self.assertEqual(context.current_tariff_bucket, "overnight")

    def test_tariff_transitions_classify_high_and_waiting_context(self):
        context = self.build(now=datetime(2026, 6, 27, 18, tzinfo=LOCAL))

        self.assertEqual(context.tariff, TariffContext.HIGHEST_COST)
        self.assertEqual(context.charging, ChargingContext.WAITING_FOR_CHEAPER_WINDOW)
        self.assertEqual(context.next_lower_cost_window, "Off peak from 9:00 PM")

    def test_billing_period_transitions_are_descriptive(self):
        new_period = self.build(
            now=datetime(2026, 6, 27, 10, tzinfo=LOCAL),
            billing_summary=Summary(reading_days=1),
        )
        ending_period = self.build(
            now=datetime(2026, 6, 27, 10, tzinfo=LOCAL),
            settings=Settings(RATES, current_billing_period_end=date(2026, 6, 29)),
        )

        self.assertEqual(new_period.billing, BillingContext.NEW_BILLING_PERIOD)
        self.assertEqual(ending_period.billing, BillingContext.BILL_END_APPROACHING)

    def test_evidence_degradation_returns_insufficient_or_historical(self):
        insufficient = self.build(
            now=datetime(2026, 6, 27, 10, tzinfo=LOCAL),
            today_summary=Summary(intervals=0),
            billing_summary=Summary(intervals=0),
            health={"status": "no readings", "report_age_minutes": None},
        )
        historical = self.build(
            now=datetime(2026, 6, 27, 10, tzinfo=LOCAL),
            health={"status": "ok", "report_age_minutes": 240},
        )

        self.assertEqual(insufficient.evidence, EvidenceContext.INSUFFICIENT)
        self.assertEqual(insufficient.solar, SolarContext.UNKNOWN)
        self.assertEqual(insufficient.confidence, ContextConfidence.UNKNOWN)
        self.assertEqual(historical.evidence, EvidenceContext.HISTORICAL_ONLY)
        self.assertEqual(historical.confidence, ContextConfidence.LOW)

    def test_household_context_serializes_enum_values(self):
        data = self.build(now=datetime(2026, 6, 27, 18, tzinfo=LOCAL)).as_dict()

        self.assertEqual(data["time"], "EVENING")
        self.assertEqual(data["tariff"], "HIGHEST_COST")
        self.assertEqual(data["confidence"], "HIGH")
        self.assertIn("data_sources_used", data)


if __name__ == "__main__":
    unittest.main()
