import unittest
from dataclasses import dataclass
from datetime import date

from collector.periods import resolve_period


@dataclass
class Settings:
    billing_period_start_day: int = 6
    current_billing_period_start: date = date(2026, 6, 6)
    previous_billing_period_start: date = date(2026, 4, 3)
    previous_billing_period_end: date = date(2026, 6, 5)


class PeriodSelectionTests(unittest.TestCase):
    def test_previous_billing_period_uses_configured_meter_read_dates(self):
        period = resolve_period("previous_billing_period", Settings(), date(2026, 6, 29))

        self.assertEqual(period.label, "Previous billing period")
        self.assertEqual(period.start, date(2026, 4, 3))
        self.assertEqual(period.end, date(2026, 6, 5))

    def test_year_to_date_starts_on_first_january(self):
        period = resolve_period("year_to_date", Settings(), date(2026, 6, 29))

        self.assertEqual(period.label, "Year to date")
        self.assertEqual(period.start, date(2026, 1, 1))
        self.assertEqual(period.end, date(2026, 6, 29))

    def test_last_calendar_year_is_full_prior_year(self):
        period = resolve_period("last_calendar_year", Settings(), date(2026, 6, 29))

        self.assertEqual(period.label, "Last calendar year")
        self.assertEqual(period.start, date(2025, 1, 1))
        self.assertEqual(period.end, date(2025, 12, 31))


if __name__ == "__main__":
    unittest.main()
