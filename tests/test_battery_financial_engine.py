import unittest

from engine.financial_engine import FinancialEngine


class Settings:
    import_rates = {
        "super_off_peak": 0.08,
        "overnight": 0.18,
        "off_peak": 0.28,
        "peak": 0.60,
    }
    export_credit_rate = 0.02


def interval(day: str, hour: int, bucket: str, grid_import: float = 0.0, solar_export: float = 0.0) -> dict:
    return {
        "measurement_time": f"{day}T{hour:02d}:00:00",
        "tariff_bucket": bucket,
        "grid_import_kwh": grid_import,
        "solar_export_kwh": solar_export,
    }


class BatteryFinancialEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = FinancialEngine()
        self.settings = Settings()

    def compare(self, rows, baseline_bill=100.0, import_kwh=40.0, export_kwh=20.0):
        return self.engine.compare_home_battery_sizes(
            rows,
            self.settings,
            baseline_bill=baseline_bill,
            baseline_grid_import_kwh=import_kwh,
            baseline_solar_exported_kwh=export_kwh,
        )

    def option(self, comparison, capacity):
        return next(item for item in comparison["options"] if item["capacity_kwh"] == capacity)

    def test_battery_options_include_baseline_and_supported_sizes(self):
        comparison = self.compare([
            interval("2026-07-01", 12, "super_off_peak", solar_export=8.0),
            interval("2026-07-01", 18, "peak", grid_import=4.0),
        ])

        self.assertTrue(comparison["available"])
        self.assertEqual(comparison["baseline"]["label"], "No battery")
        self.assertEqual([item["capacity_kwh"] for item in comparison["options"]], [5.0, 10.0, 15.0, 20.0])
        self.assertIn("charges only from verified excess solar export", comparison["methodology"])
        self.assertFalse(comparison["assumptions"]["grid_charging"])

    def test_no_excess_solar_gives_no_financial_case(self):
        comparison = self.compare([
            interval("2026-07-01", 18, "peak", grid_import=6.0),
            interval("2026-07-02", 18, "peak", grid_import=6.0),
        ], export_kwh=0.0)
        five = self.option(comparison, 5.0)

        self.assertAlmostEqual(five["period_savings"], 0.0)
        self.assertAlmostEqual(five["stored_solar_kwh"], 0.0)
        self.assertEqual(comparison["recommended"]["status"], "no_financial_case")
        self.assertFalse(comparison["data_quality"]["has_verified_excess_solar"])

    def test_daily_reset_prevents_previous_day_solar_serving_next_day_import(self):
        comparison = self.compare([
            interval("2026-07-01", 12, "super_off_peak", solar_export=10.0),
            interval("2026-07-02", 18, "peak", grid_import=10.0),
        ])
        five = self.option(comparison, 5.0)

        self.assertAlmostEqual(five["discharged_kwh"], 0.0)
        self.assertLess(five["period_savings"], 0.0)
        self.assertEqual(five["days_full"], 1)
        self.assertEqual(five["days_depleted"], 0)

    def test_peak_import_is_avoided_before_other_tariff_buckets(self):
        comparison = self.compare([
            interval("2026-07-01", 12, "super_off_peak", solar_export=5.0),
            interval("2026-07-01", 17, "off_peak", grid_import=5.0),
            interval("2026-07-01", 18, "peak", grid_import=5.0),
        ])
        five = self.option(comparison, 5.0)

        self.assertAlmostEqual(five["peak_import_avoided_kwh"], 5.0)
        self.assertAlmostEqual(five["other_import_avoided_kwh"], 0.0)
        self.assertAlmostEqual(five["discharged_kwh"], 5.0)
        self.assertAlmostEqual(five["period_savings"], 5.0 * (0.60 - 0.02))

    def test_dispatch_uses_other_non_cheapest_import_after_peak(self):
        comparison = self.compare([
            interval("2026-07-01", 12, "super_off_peak", solar_export=10.0),
            interval("2026-07-01", 16, "off_peak", grid_import=4.0),
            interval("2026-07-01", 18, "peak", grid_import=3.0),
            interval("2026-07-01", 22, "super_off_peak", grid_import=5.0),
        ])
        ten = self.option(comparison, 10.0)

        self.assertAlmostEqual(ten["peak_import_avoided_kwh"], 3.0)
        self.assertAlmostEqual(ten["other_import_avoided_kwh"], 4.0)
        self.assertNotIn("super_off_peak", ten["discharge_buckets"])
        self.assertAlmostEqual(ten["discharged_kwh"], 7.0)

    def test_no_simultaneous_charge_and_discharge_for_exporting_interval(self):
        comparison = self.compare([
            interval("2026-07-01", 12, "peak", grid_import=5.0, solar_export=5.0),
            interval("2026-07-01", 18, "peak", grid_import=3.0),
        ])
        five = self.option(comparison, 5.0)

        self.assertAlmostEqual(five["stored_solar_kwh"], 5.0)
        self.assertAlmostEqual(five["discharged_kwh"], 3.0)
        self.assertAlmostEqual(five["remaining_grid_import_kwh"], 37.0)

    def test_diminishing_return_recommendation_avoids_oversizing(self):
        rows = []
        for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"):
            rows.append(interval(day, 12, "super_off_peak", solar_export=6.0))
            rows.append(interval(day, 18, "peak", grid_import=5.0))
        comparison = self.compare(rows, baseline_bill=120.0, import_kwh=20.0, export_kwh=24.0)

        self.assertEqual(comparison["recommended"]["capacity_kwh"], 5.0)
        self.assertEqual(comparison["recommended"]["diminishing_return_capacity_kwh"], 10.0)
        self.assertLessEqual(self.option(comparison, 10.0)["marginal_savings"], self.option(comparison, 5.0)["period_savings"] * 0.15)


if __name__ == "__main__":
    unittest.main()
