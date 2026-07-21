import unittest
from datetime import datetime, time

from engine.ev_charging_inference import (
    EVChargingInferenceConfig,
    infer_active_charging_interval,
    split_energy_by_tariff,
)


RATES = {
    "super_off_peak": 0.08,
    "overnight": 0.19,
    "off_peak": 0.23,
    "peak": 0.54,
}


class EVChargingInferenceTests(unittest.TestCase):
    def setUp(self):
        self.config = EVChargingInferenceConfig(
            charger_power_kw=6.9,
            scheduled_start=time(23, 0),
            scheduled_end=time(6, 0),
            import_rates=RATES,
        )

    def infer(self, connected: str, disconnected: str, energy: float):
        return infer_active_charging_interval(
            datetime.fromisoformat(connected),
            datetime.fromisoformat(disconnected) if disconnected else None,
            energy,
            self.config,
        )

    def test_vehicle_connected_before_scheduled_start_uses_schedule(self):
        estimate = self.infer("2026-07-13T16:31:41", "2026-07-14T08:56:07", 30.53)

        self.assertEqual(estimate.active_start, datetime(2026, 7, 13, 23, 0))
        self.assertLess(abs((estimate.active_finish - datetime(2026, 7, 14, 3, 25, 29)).total_seconds()), 1)
        self.assertAlmostEqual(estimate.estimated_active_duration_hours, 30.53 / 6.9)

    def test_vehicle_connected_during_configured_window_starts_at_connection(self):
        estimate = self.infer("2026-07-13T23:30:00", "2026-07-14T04:00:00", 6.9)

        self.assertEqual(estimate.active_start, datetime(2026, 7, 13, 23, 30))
        self.assertEqual(estimate.active_finish, datetime(2026, 7, 14, 0, 30))

    def test_charging_wholly_within_one_tariff_window(self):
        cost, breakdown = split_energy_by_tariff(
            datetime(2026, 7, 13, 23, 0),
            datetime(2026, 7, 14, 0, 0),
            6.9,
            RATES,
        )

        self.assertAlmostEqual(breakdown["overnight"]["kwh"], 6.9)
        self.assertAlmostEqual(cost, 6.9 * RATES["overnight"])

    def test_charging_crossing_midnight_stays_overnight(self):
        _, breakdown = split_energy_by_tariff(
            datetime(2026, 7, 13, 23, 0),
            datetime(2026, 7, 14, 3, 0),
            27.6,
            RATES,
        )

        self.assertAlmostEqual(breakdown["overnight"]["kwh"], 27.6)

    def test_charging_crossing_tariff_boundary_splits_energy(self):
        _, breakdown = split_energy_by_tariff(
            datetime(2026, 7, 14, 5, 30),
            datetime(2026, 7, 14, 6, 30),
            6.9,
            RATES,
        )

        self.assertAlmostEqual(breakdown["overnight"]["kwh"], 3.45)
        self.assertAlmostEqual(breakdown["off_peak"]["kwh"], 3.45)

    def test_charging_crossing_multiple_tariff_boundaries_splits_energy(self):
        _, breakdown = split_energy_by_tariff(
            datetime(2026, 7, 14, 5, 30),
            datetime(2026, 7, 14, 9, 30),
            27.6,
            RATES,
        )

        self.assertAlmostEqual(breakdown["overnight"]["kwh"], 3.45)
        self.assertAlmostEqual(breakdown["off_peak"]["kwh"], 20.7)
        self.assertAlmostEqual(breakdown["super_off_peak"]["kwh"], 3.45)

    def test_vehicle_left_connected_after_finish_is_not_charged_until_disconnect(self):
        estimate = self.infer("2026-07-13T16:31:41", "2026-07-14T08:56:07", 6.9)

        self.assertEqual(estimate.active_start, datetime(2026, 7, 13, 23, 0))
        self.assertEqual(estimate.active_finish, datetime(2026, 7, 14, 0, 0))

    def test_active_duration_longer_than_connected_interval_is_clamped(self):
        estimate = self.infer("2026-07-13T23:00:00", "2026-07-14T00:00:00", 30.53)

        self.assertEqual(estimate.active_start, datetime(2026, 7, 13, 23, 0))
        self.assertEqual(estimate.active_finish, datetime(2026, 7, 14, 0, 0))
        self.assertIn("active_duration_clamped_to_connected_interval", estimate.diagnostics)

    def test_zero_energy_does_not_create_active_interval(self):
        estimate = self.infer("2026-07-13T23:00:00", "2026-07-14T00:00:00", 0.0)

        self.assertIsNone(estimate.active_start)
        self.assertIsNone(estimate.active_finish)

    def test_missing_disconnect_does_not_create_active_interval(self):
        estimate = self.infer("2026-07-13T23:00:00", "", 6.9)

        self.assertIsNone(estimate.active_start)
        self.assertIsNone(estimate.active_finish)

    def test_tariff_allocation_sums_to_imported_energy(self):
        _, breakdown = split_energy_by_tariff(
            datetime(2026, 7, 13, 23, 0),
            datetime(2026, 7, 14, 3, 25, 29),
            30.53,
            RATES,
        )

        self.assertAlmostEqual(sum(bucket["kwh"] for bucket in breakdown.values()), 30.53)


if __name__ == "__main__":
    unittest.main()
