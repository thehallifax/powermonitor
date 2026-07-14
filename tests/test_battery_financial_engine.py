import unittest
from datetime import date, timedelta

from engine.financial_engine import FinancialEngine, au_season_for_date


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


def seasonal_days(start: date, count: int, solar_export: float = 12.0, grid_import: float = 12.0) -> list[dict]:
    rows = []
    for offset in range(count):
        day = (start + timedelta(days=offset)).isoformat()
        rows.append(interval(day, 12, "super_off_peak", solar_export=solar_export))
        rows.append(interval(day, 18, "peak", grid_import=grid_import))
    return rows


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
        self.assertIn("solar_only", comparison["assumptions"]["strategies"])
        self.assertIn("solar_plus_grid_arbitrage", comparison["assumptions"]["strategies"])
        self.assertIn("cheapest", comparison["assumptions"]["grid_charging"])
        self.assertEqual(comparison["assumptions"]["round_trip_efficiency_pct"], 90.0)
        self.assertIn("diagnostics", comparison)
        self.assertIn("opportunity_metrics", comparison)
        self.assertIn("strategies", comparison)
        self.assertEqual([item["capacity_kwh"] for item in comparison["strategies"]["solar_only"]["options"]], [5.0, 10.0, 15.0, 20.0])
        self.assertEqual([item["capacity_kwh"] for item in comparison["strategies"]["solar_plus_grid_arbitrage"]["options"]], [5.0, 10.0, 15.0, 20.0])
        self.assertEqual(comparison["options"][0]["daily_diagnostics"][0]["starting_state_of_charge_kwh"], 0.0)
        self.assertEqual(comparison["options"][0]["daily_diagnostics"][0]["energy_conservation_check"], "pass")

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
        self.assertAlmostEqual(five["daily_diagnostics"][0]["ending_state_of_charge_kwh"], 5.0)
        self.assertAlmostEqual(five["daily_diagnostics"][1]["starting_state_of_charge_kwh"], 0.0)

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
        row = five["daily_diagnostics"][0]
        self.assertAlmostEqual(row["battery_discharge_into_peak_kwh"], 5.0)
        self.assertAlmostEqual(row["battery_discharge_into_other_non_cheapest_kwh"], 0.0)
        self.assertAlmostEqual(row["estimated_savings"], five["period_savings"])

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
        row = ten["daily_diagnostics"][0]
        self.assertAlmostEqual(row["remaining_cheapest_window_import_kwh"], 5.0)
        self.assertAlmostEqual(row["total_battery_discharge_kwh"], 7.0)
        self.assertEqual(row["energy_conservation_check"], "pass")

    def test_no_simultaneous_charge_and_discharge_for_exporting_interval(self):
        comparison = self.compare([
            interval("2026-07-01", 12, "peak", grid_import=5.0, solar_export=5.0),
            interval("2026-07-01", 18, "peak", grid_import=3.0),
        ])
        five = self.option(comparison, 5.0)

        self.assertAlmostEqual(five["stored_solar_kwh"], 5.0)
        self.assertAlmostEqual(five["discharged_kwh"], 3.0)
        self.assertAlmostEqual(five["remaining_grid_import_kwh"], 37.0)
        row = five["daily_diagnostics"][0]
        self.assertAlmostEqual(row["charged_into_battery_kwh"], 5.0)
        self.assertAlmostEqual(row["total_battery_discharge_kwh"], 3.0)
        self.assertEqual(row["energy_conservation_check"], "pass")
        self.assertEqual(row["warnings"], [])

    def test_diminishing_return_recommendation_avoids_oversizing(self):
        rows = []
        for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"):
            rows.append(interval(day, 12, "super_off_peak", solar_export=6.0))
            rows.append(interval(day, 18, "peak", grid_import=5.0))
        comparison = self.compare(rows, baseline_bill=120.0, import_kwh=20.0, export_kwh=24.0)

        self.assertEqual(comparison["recommended"]["capacity_kwh"], 5.0)
        self.assertEqual(comparison["recommended"]["diminishing_return_capacity_kwh"], 10.0)
        self.assertLessEqual(self.option(comparison, 10.0)["marginal_savings"], self.option(comparison, 5.0)["period_savings"] * 0.15)
        self.assertEqual(self.option(comparison, 5.0)["recommendation_signal"], "Recommended")
        self.assertIn(self.option(comparison, 20.0)["recommendation_signal"], {"Limited additional benefit", "Likely oversized for this usage pattern"})

    def test_rejected_solar_and_remaining_export_reconcile_when_capacity_is_full(self):
        comparison = self.compare([
            interval("2026-07-01", 12, "super_off_peak", solar_export=8.0),
            interval("2026-07-01", 18, "peak", grid_import=3.0),
        ], baseline_bill=30.0, import_kwh=3.0, export_kwh=8.0)
        five = self.option(comparison, 5.0)
        row = five["daily_diagnostics"][0]

        self.assertAlmostEqual(row["verified_solar_export_available_kwh"], 8.0)
        self.assertAlmostEqual(row["charged_into_battery_kwh"], 5.0)
        self.assertAlmostEqual(row["rejected_solar_because_battery_full_kwh"], 3.0)
        self.assertAlmostEqual(row["remaining_solar_export_kwh"], 3.0)
        self.assertLessEqual(row["charged_into_battery_kwh"], row["verified_solar_export_available_kwh"])

    def test_diagnostics_summary_flags_energy_conservation_warnings(self):
        bad_rows = [{
            "date": "2026-07-01",
            "verified_solar_export_available_kwh": 1.0,
            "charged_into_battery_kwh": 1.0,
            "rejected_solar_because_battery_full_kwh": 0.0,
            "peak_import_before_battery_kwh": 0.0,
            "other_non_cheapest_import_before_battery_kwh": 0.0,
            "cheapest_window_import_before_battery_kwh": 0.0,
            "battery_discharge_into_peak_kwh": 2.0,
            "battery_discharge_into_other_non_cheapest_kwh": 0.0,
            "total_battery_discharge_kwh": 2.0,
            "ending_state_of_charge_kwh": 0.0,
            "remaining_peak_import_kwh": 0.0,
            "remaining_other_non_cheapest_import_kwh": 0.0,
            "remaining_cheapest_window_import_kwh": 0.0,
            "remaining_solar_export_kwh": 0.0,
            "estimated_savings": 0.0,
            "warnings": ["discharged_more_than_stored"],
        }]

        summary = self.engine._battery_diagnostics_summary(bad_rows)

        self.assertEqual(summary["energy_conservation_check"], "fail")
        self.assertIn("discharged_more_than_stored", summary["warnings"][0])

    def test_grid_arbitrage_charges_only_when_loss_adjusted_spread_is_positive(self):
        comparison = self.compare([
            interval("2026-07-01", 12, "super_off_peak", solar_export=2.0),
            interval("2026-07-01", 18, "peak", grid_import=5.0),
            interval("2026-07-01", 20, "off_peak", grid_import=4.0),
        ], baseline_bill=20.0, import_kwh=9.0, export_kwh=2.0)
        solar = self.option(comparison, 5.0)
        grid = comparison["strategies"]["solar_plus_grid_arbitrage"]["options"][0]
        row = grid["daily_diagnostics"][0]

        self.assertAlmostEqual(solar["period_savings"], 2.0 * (0.60 - 0.02))
        self.assertAlmostEqual(grid["grid_energy_used_to_charge_battery_kwh"], 3.0 / 0.90)
        self.assertAlmostEqual(grid["grid_charged_delivered_kwh"], 3.0)
        self.assertAlmostEqual(grid["charging_losses_kwh"], (3.0 / 0.90) - 3.0)
        self.assertGreater(grid["period_savings"], solar["period_savings"])
        self.assertAlmostEqual(grid["additional_savings_from_grid_charging"], grid["period_savings"] - solar["period_savings"])
        self.assertEqual(row["grid_charge_tariff_bucket"], "super_off_peak")
        self.assertAlmostEqual(row["grid_charge_rate"], 0.08)
        self.assertEqual(row["energy_conservation_check"], "pass")
        self.assertEqual(row["warnings"], [])
        self.assertIn("peak", row["discharge_destination_tariffs"])

    def test_grid_arbitrage_does_not_charge_when_peak_value_is_below_effective_cost(self):
        expensive_settings = Settings()
        expensive_settings.import_rates = {
            "super_off_peak": 0.50,
            "off_peak": 0.52,
            "peak": 0.55,
        }
        expensive_settings.export_credit_rate = 0.02
        expensive_settings.battery_round_trip_efficiency = 0.80
        result = self.engine.compare_home_battery_sizes(
            [
                interval("2026-07-01", 12, "super_off_peak", solar_export=0.0),
                interval("2026-07-01", 18, "peak", grid_import=5.0),
            ],
            expensive_settings,
            baseline_bill=10.0,
            baseline_grid_import_kwh=5.0,
            baseline_solar_exported_kwh=0.0,
            capacities_kwh=(5.0,),
        )
        grid = result["strategies"]["solar_plus_grid_arbitrage"]["options"][0]

        self.assertAlmostEqual(grid["grid_energy_used_to_charge_battery_kwh"], 0.0)
        self.assertAlmostEqual(grid["period_savings"], 0.0)
        self.assertAlmostEqual(grid["average_avoided_grid_rate_per_discharged_kwh"], 0.0)

    def test_grid_arbitrage_respects_capacity_and_daily_limits(self):
        limited_settings = Settings()
        limited_settings.battery_charge_limit_kwh_per_day = 3.0
        limited_settings.battery_discharge_limit_kwh_per_day = 2.0
        result = self.engine.compare_home_battery_sizes(
            [
                interval("2026-07-01", 12, "super_off_peak", solar_export=1.0),
                interval("2026-07-01", 18, "peak", grid_import=10.0),
            ],
            limited_settings,
            baseline_bill=20.0,
            baseline_grid_import_kwh=10.0,
            baseline_solar_exported_kwh=1.0,
            capacities_kwh=(5.0,),
        )
        grid = result["strategies"]["solar_plus_grid_arbitrage"]["options"][0]
        row = grid["daily_diagnostics"][0]

        self.assertAlmostEqual(row["solar_charged_kwh"], 1.0)
        self.assertAlmostEqual(row["grid_charged_delivered_kwh"], 1.0)
        self.assertAlmostEqual(row["total_battery_discharge_kwh"], 2.0)
        self.assertLessEqual(row["charged_into_battery_kwh"], 3.0)

    def test_solar_only_strategy_values_remain_unchanged_when_grid_strategy_is_added(self):
        rows = [
            interval("2026-07-01", 12, "super_off_peak", solar_export=5.0),
            interval("2026-07-01", 18, "peak", grid_import=5.0),
        ]
        comparison = self.compare(rows, baseline_bill=20.0, import_kwh=5.0, export_kwh=5.0)
        top_level = self.option(comparison, 5.0)
        strategy = comparison["strategies"]["solar_only"]["options"][0]

        self.assertAlmostEqual(top_level["period_savings"], 5.0 * (0.60 - 0.02))
        self.assertEqual(top_level, strategy)

    def test_au_season_detection_uses_australian_seasons(self):
        self.assertEqual(au_season_for_date(date(2026, 1, 15)), "summer")
        self.assertEqual(au_season_for_date(date(2026, 4, 15)), "autumn")
        self.assertEqual(au_season_for_date(date(2026, 7, 15)), "winter")
        self.assertEqual(au_season_for_date(date(2026, 10, 15)), "spring")

    def test_seasonal_battery_model_weights_included_interval_days(self):
        seasonal = []
        for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
            seasonal.append(interval(day, 12, "super_off_peak", solar_export=10.0))
            seasonal.append(interval(day, 18, "peak", grid_import=12.0))
        for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05", "2026-07-06"):
            seasonal.append(interval(day, 12, "super_off_peak", solar_export=10.0))
            seasonal.append(interval(day, 18, "peak", grid_import=12.0))
        comparison = self.engine.compare_home_battery_sizes(
            [interval("2026-07-01", 12, "super_off_peak", solar_export=6.0), interval("2026-07-01", 18, "peak", grid_import=6.0)],
            self.settings,
            baseline_bill=20.0,
            baseline_grid_import_kwh=6.0,
            baseline_solar_exported_kwh=6.0,
            seasonal_intervals=seasonal,
        )

        seasonal_model = comparison["seasonal_model"]

        self.assertTrue(seasonal_model["available"])
        self.assertEqual(set(seasonal_model["annual_weighting"]), {"summer", "winter"})
        self.assertAlmostEqual(seasonal_model["annual_weighting"]["summer"], 3 / 9)
        self.assertAlmostEqual(seasonal_model["annual_weighting"]["winter"], 6 / 9)
        self.assertEqual(seasonal_model["annual_recommendation"]["capacity_kwh"], 15.0)
        mismatch = seasonal_model["recommendation_mismatch"]
        self.assertTrue(mismatch["exists"])
        self.assertEqual(mismatch["annual_size_kwh"], 15.0)
        self.assertGreater(mismatch["additional_annual_saving"], 0.0)
        self.assertIn("combined annual model", mismatch["reason"])
        self.assertEqual(comparison["confidence_model"]["level"], "Low")
        self.assertEqual(comparison["battery_recommendation"]["source"], "current_period")
        self.assertIn("seasonal_model", comparison)
        self.assertIn("current_period", comparison)

    def test_high_confidence_annual_history_becomes_primary(self):
        seasonal = []
        seasonal.extend(seasonal_days(date(2026, 1, 1), 40))
        seasonal.extend(seasonal_days(date(2026, 4, 1), 40))
        seasonal.extend(seasonal_days(date(2026, 7, 1), 40))

        comparison = self.engine.compare_home_battery_sizes(
            [interval("2026-07-01", 12, "super_off_peak", solar_export=5.0), interval("2026-07-01", 18, "peak", grid_import=5.0)],
            self.settings,
            baseline_bill=20.0,
            baseline_grid_import_kwh=5.0,
            baseline_solar_exported_kwh=5.0,
            seasonal_intervals=seasonal,
        )

        recommendation = comparison["battery_recommendation"]

        self.assertEqual(comparison["confidence_model"]["level"], "High")
        self.assertEqual(recommendation["source"], "annual_historical")
        self.assertEqual(recommendation["interval_days"], 120)
        self.assertEqual(set(recommendation["seasons_included"]), {"summer", "autumn", "winter"})
        self.assertEqual(recommendation["recommended_size_kwh"], comparison["seasonal_model"]["annual_recommendation"]["capacity_kwh"])

    def test_very_high_confidence_annual_history_becomes_primary(self):
        seasonal = []
        seasonal.extend(seasonal_days(date(2026, 1, 1), 68))
        seasonal.extend(seasonal_days(date(2026, 4, 1), 68))
        seasonal.extend(seasonal_days(date(2026, 7, 1), 68))
        seasonal.extend(seasonal_days(date(2026, 10, 1), 68))

        comparison = self.engine.compare_home_battery_sizes(
            [interval("2026-07-01", 12, "super_off_peak", solar_export=5.0), interval("2026-07-01", 18, "peak", grid_import=5.0)],
            self.settings,
            baseline_bill=20.0,
            baseline_grid_import_kwh=5.0,
            baseline_solar_exported_kwh=5.0,
            seasonal_intervals=seasonal,
        )

        self.assertEqual(comparison["confidence_model"]["level"], "Very High")
        self.assertEqual(comparison["battery_recommendation"]["source"], "annual_historical")
        self.assertEqual(set(comparison["battery_recommendation"]["seasons_included"]), {"summer", "autumn", "winter", "spring"})

    def test_medium_history_falls_back_to_current_period(self):
        seasonal = seasonal_days(date(2026, 1, 1), 30) + seasonal_days(date(2026, 7, 1), 20)

        comparison = self.engine.compare_home_battery_sizes(
            [interval("2026-07-01", 12, "super_off_peak", solar_export=5.0), interval("2026-07-01", 18, "peak", grid_import=5.0)],
            self.settings,
            baseline_bill=20.0,
            baseline_grid_import_kwh=5.0,
            baseline_solar_exported_kwh=5.0,
            seasonal_intervals=seasonal,
        )

        self.assertEqual(comparison["confidence_model"]["level"], "Medium")
        self.assertEqual(comparison["battery_recommendation"]["source"], "current_period")
        self.assertTrue(comparison["battery_recommendation"]["warnings"])

    def test_seasonal_model_ignores_insufficient_seasons_and_aggregate_rows(self):
        rows = [
            {**interval("2026-01-01", 12, "super_off_peak", solar_export=15.0), "source_resolution": "daily_aggregate"},
            {**interval("2026-01-02", 12, "super_off_peak", solar_export=15.0), "source_resolution": "daily_aggregate"},
        ]

        model = self.engine.seasonal_battery_model(rows, self.settings)

        self.assertFalse(model["available"])
        self.assertEqual(model["confidence_model"]["level"], "Insufficient history")
        self.assertIn("interval household energy flows", model["summary"])

    def test_aggregate_only_history_cannot_be_primary(self):
        rows = [
            {**interval("2026-01-01", 12, "super_off_peak", solar_export=12.0), "source_resolution": "daily_aggregate"},
            {**interval("2026-07-01", 18, "peak", grid_import=12.0), "source_resolution": "daily_aggregate"},
        ]

        comparison = self.engine.compare_home_battery_sizes(
            [interval("2026-07-01", 12, "super_off_peak", solar_export=5.0), interval("2026-07-01", 18, "peak", grid_import=5.0)],
            self.settings,
            baseline_bill=20.0,
            baseline_grid_import_kwh=5.0,
            baseline_solar_exported_kwh=5.0,
            seasonal_intervals=rows,
        )

        self.assertEqual(comparison["battery_recommendation"]["source"], "current_period")
        self.assertIn("interval household energy flows", " ".join(comparison["battery_recommendation"]["warnings"]))


if __name__ == "__main__":
    unittest.main()
