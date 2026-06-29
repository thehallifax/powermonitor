import unittest
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from engine.context_engine import ContextEngine, TariffContext
from engine.household_state_engine import HouseholdStateEngine
from engine.models import CapabilityStatus, HouseholdCapabilitySummary


LOCAL = ZoneInfo("Australia/Perth")


@dataclass
class Summary:
    intervals: int = 12
    reading_days: int = 8
    supply_days: int = 24
    grid_import_kwh: float = 3.0
    solar_generated_kwh: float = 8.0
    solar_used_kwh: float = 5.0
    solar_exported_kwh: float = 3.0
    solar_export_pct: float = 37.5
    ev_charging_source: str = "ocular_export"
    ev_charging_kwh: float = 18.0
    net_cost: float = 72.0


@dataclass
class Settings:
    import_rates: dict
    current_billing_period_start: date = date(2026, 6, 6)
    current_billing_period_end: date = date(2026, 6, 29)


RATES = {
    "super_off_peak": 0.086151,
    "overnight": 0.193841,
    "off_peak": 0.236916,
    "peak": 0.538446,
}


def capability_summary(solar=True, ev=True, battery=False):
    return HouseholdCapabilitySummary(
        household_id="default_household",
        generated_at="2026-06-29T10:00:00+08:00",
        capabilities=[
            CapabilityStatus("solar", "Solar", present=solar, source="provider:solaredge", reason="Solar state"),
            CapabilityStatus("ev", "EV", present=ev, source="provider:ocular_export", reason="EV state"),
            CapabilityStatus("battery", "Battery", present=battery, source="provider:battery", reason="Battery state"),
        ],
    )


class HouseholdStateEngineTests(unittest.TestCase):
    def build(self, *, now, today=None, billing=None, settings=None, health=None, capabilities=None):
        return HouseholdStateEngine().build(
            today_summary=today or Summary(),
            billing_summary=billing or today or Summary(),
            settings=settings or Settings(RATES),
            health=health or {"status": "ok", "report_age_minutes": 5, "latest_measurement_time": "2026-06-29T09:55:00+08:00"},
            capability_summary=capabilities or capability_summary(),
            provider_observations={"count": 1, "latest": [{"provider_id": "solaredge"}]},
            promotion_summary={"latest_promotions": [{"provider_id": "solaredge"}]},
            household_id="default_household",
            now=now,
        )

    def test_household_state_creation_serializes_current_facts(self):
        state = self.build(now=datetime(2026, 6, 29, 12, tzinfo=LOCAL))
        data = state.as_dict()

        self.assertEqual(data["household_id"], "default_household")
        self.assertEqual(data["tariff"]["active_bucket"], "super_off_peak")
        self.assertEqual(data["power"]["import_export_state"], "importing")
        self.assertTrue(data["solar"]["present"])
        self.assertTrue(data["ev"]["present"])
        self.assertEqual(data["evidence"]["confidence"], "HIGH")

    def test_tariff_transitions_are_canonical(self):
        state = self.build(now=datetime(2026, 6, 29, 18, tzinfo=LOCAL))

        self.assertEqual(state.tariff.active_bucket, "peak")
        self.assertEqual(state.tariff.active_rate, RATES["peak"])

    def test_billing_transition_tracks_day_of_period(self):
        state = self.build(now=datetime(2026, 6, 29, 10, tzinfo=LOCAL))

        self.assertEqual(state.billing.period_start, "2026-06-06")
        self.assertEqual(state.billing.period_end, "2026-06-29")
        self.assertEqual(state.billing.day_of_period, 24)

    def test_provider_and_collector_health_changes_degrade_evidence(self):
        state = self.build(
            now=datetime(2026, 6, 29, 10, tzinfo=LOCAL),
            health={"status": "SolarEdge session expired", "report_age_minutes": 240, "failure_type": "auth"},
        )

        self.assertFalse(state.collector.healthy)
        self.assertEqual(state.evidence.evidence_context, "INSUFFICIENT")
        self.assertEqual(state.summary.current_status, "limited")

    def test_evidence_degradation_for_stale_data(self):
        state = self.build(
            now=datetime(2026, 6, 29, 10, tzinfo=LOCAL),
            health={"status": "ok", "report_age_minutes": 240},
        )

        self.assertEqual(state.evidence.evidence_context, "HISTORICAL_ONLY")
        self.assertEqual(state.evidence.confidence, "LOW")

    def test_context_engine_consumes_household_state(self):
        state = self.build(now=datetime(2026, 6, 29, 18, tzinfo=LOCAL))
        context = ContextEngine().build(
            today_summary=Summary(),
            billing_summary=Summary(),
            settings=Settings(RATES),
            health={"status": "ok", "report_age_minutes": 5},
            now=datetime(2026, 6, 29, 18, tzinfo=LOCAL),
            household_state=state,
        )

        self.assertEqual(context.current_tariff_bucket, "peak")
        self.assertEqual(context.tariff, TariffContext.HIGHEST_COST)
        self.assertIn("Household State Engine", context.data_sources_used)


if __name__ == "__main__":
    unittest.main()
