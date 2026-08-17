import unittest
from dataclasses import dataclass
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from collector.periods import resolve_period
from engine.billing_period_engine import resolve_billing_cycle
from engine.financial_engine import FinancialEngine
from engine.historical_comparison import resolve_historical_comparison


@dataclass
class Settings:
    billing_period_start_day: int = 1
    current_billing_period_start: date = date(2026, 6, 6)
    current_billing_period_end: date = None
    previous_billing_period_start: date = date(2026, 4, 3)
    previous_billing_period_end: date = date(2026, 6, 5)


class BillingPeriodRolloverTests(unittest.TestCase):
    def test_production_regression_rolls_to_active_64_day_cycle(self):
        cycle = resolve_billing_cycle(Settings(), date(2026, 8, 17))

        self.assertEqual(cycle.start, date(2026, 8, 9))
        self.assertEqual(cycle.data_through, date(2026, 8, 17))
        self.assertEqual(cycle.expected_end, date(2026, 10, 11))
        self.assertEqual((cycle.previous_start, cycle.previous_end), (date(2026, 6, 6), date(2026, 8, 8)))
        self.assertEqual(cycle.cadence_days, 64)
        self.assertEqual(cycle.boundary, "predicted")

    def test_first_last_and_day_after_expected_boundary(self):
        settings = Settings()
        last_day = resolve_billing_cycle(settings, date(2026, 8, 8))
        first_day = resolve_billing_cycle(settings, date(2026, 8, 9))

        self.assertEqual(last_day.start, date(2026, 6, 6))
        self.assertEqual(last_day.expected_end, date(2026, 8, 8))
        self.assertEqual(first_day.start, date(2026, 8, 9))
        self.assertEqual(first_day.data_through, date(2026, 8, 9))

    def test_multiple_cycles_elapsed(self):
        cycle = resolve_billing_cycle(Settings(), date(2026, 12, 20))

        self.assertEqual(cycle.start, date(2026, 12, 15))
        self.assertEqual(cycle.expected_end, date(2027, 2, 16))
        self.assertLessEqual(cycle.start, cycle.data_through)
        self.assertLessEqual(cycle.data_through, cycle.expected_end)

    def test_monthly_fallback_handles_year_and_leap_february(self):
        settings = Settings(
            billing_period_start_day=28,
            current_billing_period_start=None,
            previous_billing_period_start=None,
            previous_billing_period_end=None,
        )

        february = resolve_billing_cycle(settings, date(2028, 2, 29))
        january = resolve_billing_cycle(settings, date(2028, 1, 3))
        self.assertEqual((february.start, february.expected_end), (date(2028, 2, 28), date(2028, 3, 27)))
        self.assertEqual((january.start, january.expected_end), (date(2027, 12, 28), date(2028, 1, 27)))

    def test_authoritative_previous_period_is_preserved_until_rollover(self):
        cycle = resolve_billing_cycle(Settings(), date(2026, 7, 1))

        self.assertEqual((cycle.previous_start, cycle.previous_end), (date(2026, 4, 3), date(2026, 6, 5)))
        self.assertEqual(cycle.boundary, "configured")

    def test_later_authoritative_boundary_reanchors_prediction(self):
        settings = Settings(
            current_billing_period_start=date(2026, 8, 11),
            previous_billing_period_start=date(2026, 6, 6),
            previous_billing_period_end=date(2026, 8, 10),
        )
        cycle = resolve_billing_cycle(settings, date(2026, 8, 17))

        self.assertEqual(cycle.start, date(2026, 8, 11))
        self.assertEqual(cycle.expected_end, date(2026, 10, 15))
        self.assertEqual((cycle.previous_start, cycle.previous_end), (date(2026, 6, 6), date(2026, 8, 10)))

    def test_current_and_previous_selectors_share_canonical_boundaries(self):
        current = resolve_period("current_billing_period", Settings(), date(2026, 8, 17))
        previous = resolve_period("previous_billing_period", Settings(), date(2026, 8, 17))

        self.assertEqual((current.start, current.end, current.expected_end), (date(2026, 8, 9), date(2026, 8, 17), date(2026, 10, 11)))
        self.assertEqual((previous.start, previous.end), (date(2026, 6, 6), date(2026, 8, 8)))

    def test_custom_period_is_unchanged(self):
        custom = resolve_period("custom", Settings(), date(2026, 8, 17), "2025-02-03", "2025-03-07")
        self.assertEqual((custom.start, custom.end), (date(2025, 2, 3), date(2025, 3, 7)))

    def test_historical_comparison_uses_canonical_previous_cycle(self):
        current = resolve_period("current_billing_period", Settings(), date(2026, 8, 17))
        comparison = resolve_historical_comparison(
            "previous_billing_period",
            current,
            today=date(2026, 8, 17),
            settings=Settings(),
        )

        self.assertEqual(comparison["primary_period"]["start"], "2026-08-09")
        self.assertEqual(comparison["comparison_period"]["start"], "2026-06-06")
        self.assertEqual(comparison["comparison_period"]["end"], "2026-08-08")
        self.assertIn("partial", comparison["partial_period_warning"].lower())

    def test_projection_uses_only_active_cycle_and_future_expected_end(self):
        summary = SimpleNamespace(
            start=date(2026, 8, 9),
            end=date(2026, 8, 17),
            supply_days=9,
            average_daily_net_cost=4.0,
        )
        projected, expected_end, basis, _impact = FinancialEngine().project_bill(summary, Settings())

        self.assertEqual(projected, 256.0)
        self.assertEqual(expected_end, date(2026, 10, 11))
        self.assertEqual(basis, "previous period length")

    def test_local_date_boundary_uses_household_timezone(self):
        utc = datetime(2026, 8, 8, 16, 1, tzinfo=ZoneInfo("UTC"))
        perth_day = utc.astimezone(ZoneInfo("Australia/Perth")).date()
        cycle = resolve_billing_cycle(Settings(), perth_day)

        self.assertEqual(perth_day, date(2026, 8, 9))
        self.assertEqual(cycle.start, date(2026, 8, 9))

    def test_resolution_payload_exposes_diagnostic_provenance(self):
        payload = resolve_billing_cycle(Settings(), date(2026, 8, 17)).as_dict()

        self.assertEqual(payload["active_billing_period_start"], "2026-08-09")
        self.assertEqual(payload["data_through"], "2026-08-17")
        self.assertEqual(payload["expected_billing_period_end"], "2026-10-11")
        self.assertEqual(payload["anchor_period"]["start"], "2026-06-06")
        self.assertEqual(payload["configured_cadence"], "64 days")
        self.assertTrue(payload["current_boundary_predicted"])


if __name__ == "__main__":
    unittest.main()
