import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from engine.behaviour_engine import BehaviourConfidence, BehaviourEngine


LOCAL = ZoneInfo("Australia/Perth")


def grid_rows(start: datetime, days: int):
    rows = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        weekend = day.weekday() >= 5
        for hour in range(24):
            watts = 260 if hour < 5 and not weekend else 320 if hour < 5 else 900
            kwh = 0.07 if hour < 5 and not weekend else 0.09 if hour < 5 else 0.22
            if hour == 7:
                kwh = 0.55
                watts = 1800
            if hour == 19:
                kwh = 0.8
                watts = 2400
            rows.append(
                {
                    "measurement_time": day.replace(hour=hour, minute=0).isoformat(),
                    "tariff_bucket": "overnight" if hour < 6 else "peak" if 15 <= hour < 21 else "off_peak",
                    "grid_to_home_w": watts,
                    "grid_to_home_kwh": kwh,
                }
            )
    return rows


def solar_rows(start: datetime, days: int):
    rows = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        sunny = offset % 2 == 0
        for hour in range(8, 17):
            generated = 1.2 if sunny else 0.25
            used = 0.55 if sunny else 0.2
            rows.append(
                {
                    "measurement_time": day.replace(hour=hour, minute=0).isoformat(),
                    "production_kwh": generated,
                    "solar_to_home_kwh": used,
                    "solar_to_grid_kwh": max(generated - used, 0.0),
                }
            )
    return rows


def ev_sessions(start: datetime):
    return [
        {
            "start_time": (start + timedelta(days=offset, hours=23)).isoformat(),
            "end_time": (start + timedelta(days=offset + 1, hours=1)).isoformat(),
            "energy_kwh": 12.0,
        }
        for offset in (0, 2, 4, 7, 9, 11, 14, 16)
    ]


class BehaviourEngineTests(unittest.TestCase):
    def test_learns_weekday_weekend_and_daily_patterns(self):
        start = datetime(2026, 6, 1, tzinfo=LOCAL)
        behaviour = BehaviourEngine().build(
            grid_intervals=grid_rows(start, 28),
            solar_intervals=solar_rows(start, 28),
            ev_sessions=ev_sessions(start),
            generated_at=start,
        )

        self.assertEqual(behaviour.confidence, BehaviourConfidence.HIGH)
        self.assertLess(
            behaviour.baseload.weekday_baseload_watts,
            behaviour.baseload.weekend_baseload_watts,
        )
        self.assertEqual(behaviour.daily_usage.typical_wake_time, "07:00")
        self.assertEqual(behaviour.daily_usage.evening_peak_time, "19:00")
        self.assertEqual(behaviour.ev.preferred_charging_window, "23:00-00:00")
        self.assertEqual(behaviour.ev.charging_consistency, "high")
        self.assertEqual(behaviour.solar.export_tendency, "moderate")

    def test_sparse_history_degrades_confidence(self):
        start = datetime(2026, 6, 1, tzinfo=LOCAL)
        behaviour = BehaviourEngine().build(
            grid_intervals=grid_rows(start, 2),
            solar_intervals=[],
            ev_sessions=[],
            generated_at=start,
        )

        self.assertEqual(behaviour.confidence, BehaviourConfidence.LOW)
        self.assertEqual(behaviour.solar.confidence, BehaviourConfidence.UNKNOWN)
        self.assertEqual(behaviour.ev.confidence, BehaviourConfidence.UNKNOWN)

    def test_empty_history_serializes_unknown_profiles(self):
        behaviour = BehaviourEngine().build(
            grid_intervals=[],
            solar_intervals=[],
            ev_sessions=[],
        )
        payload = behaviour.as_dict()

        self.assertEqual(payload["confidence"], "UNKNOWN")
        self.assertIsNone(payload["baseload"]["overnight_baseload_watts"])
        self.assertEqual(payload["ev"]["charging_consistency"], "unknown")
        self.assertIn("daily_usage", payload)


if __name__ == "__main__":
    unittest.main()
