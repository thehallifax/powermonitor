import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import report


class Settings:
    import_rates = {
        "super_off_peak": 0.08,
        "overnight": 0.19,
        "off_peak": 0.23,
        "peak": 0.54,
    }


def session(
    transaction_id,
    start_time,
    end_time,
    duration,
    *,
    energy=10.0,
    cost=2.0,
    max_power=None,
    max_power_source="unavailable",
):
    breakdown = {bucket: {"kwh": 0.0, "cost": 0.0} for bucket in report.BUCKET_ORDER}
    breakdown["overnight"] = {"kwh": energy, "cost": cost}
    return {
        "transaction_id": transaction_id,
        "date": start_time[:10],
        "start_time": start_time,
        "end_time": end_time,
        "start_time_display": start_time[11:16],
        "end_time_display": end_time[11:16],
        "duration_minutes": duration,
        "duration_display": report.format_duration(duration),
        "energy_kwh": energy,
        "cost": cost,
        "cost_per_kwh": cost / energy,
        "source": "ocular_export",
        "cost_partial": False,
        "max_power_kw": max_power,
        "max_power_source": max_power_source,
        "dominant_tariff": "overnight",
        "dominant_tariff_label": "11pm-6am overnight",
        "tariff_breakdown": breakdown,
    }


class EvAnalyticsTests(unittest.TestCase):
    def test_average_clock_time_wraps_around_midnight(self):
        analytics = report.ev_session_analytics(
            [
                session("a", "2026-06-27T23:40:00+08:00", "2026-06-28T23:50:00+08:00", 10),
                session("b", "2026-06-28T00:10:00+08:00", "2026-06-28T00:20:00+08:00", 10),
            ],
            Settings(),
        )

        self.assertEqual(analytics["insights"]["average_start_time"], "23:55")
        self.assertEqual(analytics["insights"]["average_finish_time"], "00:05")

    def test_highest_power_requires_source_supplied_power(self):
        analytics = report.ev_session_analytics(
            [
                session("a", "2026-06-27T10:00:00+08:00", "2026-06-27T12:00:00+08:00", 120, energy=22),
                session("b", "2026-06-27T13:00:00+08:00", "2026-06-27T13:30:00+08:00", 30, energy=6),
            ],
            Settings(),
        )

        self.assertIsNone(analytics["insights"]["highest_power_session"])
        self.assertFalse(analytics["insights"]["highest_power_available"])

    def test_longest_and_average_duration_use_reliable_duration_values(self):
        analytics = report.ev_session_analytics(
            [
                session("short", "2026-06-27T10:00:00+08:00", "2026-06-27T11:00:00+08:00", 60),
                session("long", "2026-06-27T12:00:00+08:00", "2026-06-27T15:00:00+08:00", 180, max_power=7.2, max_power_source="provided"),
            ],
            Settings(),
        )

        self.assertEqual(analytics["insights"]["longest_session"]["transaction_id"], "long")
        self.assertEqual(analytics["insights"]["average_duration_minutes"], 120)
        self.assertEqual(analytics["insights"]["highest_power_session"]["max_power_kw"], 7.2)


if __name__ == "__main__":
    unittest.main()
