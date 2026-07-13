import unittest
from datetime import date

from engine.historical_backfill import build_backfill_preview, most_recent_completed_au_summer


class HistoricalBackfillTests(unittest.TestCase):
    def test_previous_summer_uses_most_recent_completed_au_summer(self):
        start, end = most_recent_completed_au_summer(date(2026, 7, 13))

        self.assertEqual(start, date(2025, 12, 1))
        self.assertEqual(end, date(2026, 3, 31))

    def test_preview_does_not_treat_missing_intervals_as_battery_ready(self):
        coverage = {
            "providers": {
                "solaredge": {
                    "days": {
                        "2025-12-01": {
                            "first": "2025-12-01T00:00:00+08:00",
                            "last": "2025-12-01T23:45:00+08:00",
                            "battery_suitable": False,
                        }
                    }
                }
            }
        }

        preview = build_backfill_preview(
            coverage,
            today=date(2026, 7, 13),
            preset="previous_summer",
        )

        self.assertEqual(preview["range"]["start"], "2025-12-01")
        self.assertEqual(preview["range"]["end"], "2026-03-31")
        self.assertFalse(preview["battery_suitability"]["suitable"])
        self.assertEqual(preview["existing_coverage"]["days_with_any_reading"], 1)


if __name__ == "__main__":
    unittest.main()
