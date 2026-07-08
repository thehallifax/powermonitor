import unittest
from dataclasses import dataclass
from datetime import date, timedelta

from engine.historical_trends_engine import (
    HistoricalTrendsEngine,
    IMPROVING,
    UNCHANGED,
    WORSENING,
)


def row(day, cost, grid, solar=0.0, export=0.0):
    return {
        "day": day,
        "net_cost": cost,
        "grid_import_kwh": grid,
        "solar_generated_kwh": solar,
        "solar_exported_kwh": export,
    }


class HistoricalTrendsEngineTests(unittest.TestCase):
    def test_normal_history_builds_cost_grid_solar_export_and_self_consumption(self):
        start = date(2026, 6, 1)
        history = [
            row(start + timedelta(days=index), 10 + index, 20 + index, 8 + index, 2 + index * 0.1)
            for index in range(31)
        ]

        trends = HistoricalTrendsEngine().build(history, as_of=date(2026, 7, 2))

        self.assertEqual(trends.history_days, 31)
        self.assertTrue(trends.cost.available)
        self.assertTrue(trends.grid.available)
        self.assertTrue(trends.solar.available)
        self.assertTrue(trends.exports.available)
        self.assertTrue(trends.self_consumption.available)
        self.assertAlmostEqual(trends.cost.metrics["yesterday_cost"].value, 40.0)
        self.assertAlmostEqual(trends.cost.metrics["rolling_7_day_average_cost"].value, 37.0)
        self.assertAlmostEqual(trends.cost.metrics["rolling_30_day_average_cost"].value, 25.5)
        self.assertEqual(trends.cost.metrics["highest_daily_cost"].date, "2026-07-01")
        self.assertEqual(trends.cost.metrics["lowest_daily_cost"].date, "2026-06-01")
        self.assertEqual(trends.solar.metrics["highest_solar_day"].date, "2026-07-01")
        self.assertEqual(trends.self_consumption.metrics["best_self_consumption_day"].domain, "self_consumption")

    def test_sparse_history_uses_available_days_inside_rolling_window(self):
        history = [
            row(date(2026, 7, 1), 8.0, 10.0),
            row(date(2026, 7, 4), 12.0, 12.0),
            row(date(2026, 7, 7), 16.0, 14.0),
        ]

        trends = HistoricalTrendsEngine().build(history, as_of=date(2026, 7, 8))

        metric = trends.cost.metrics["rolling_7_day_average_cost"]
        self.assertAlmostEqual(metric.value, 12.0)
        self.assertEqual(metric.sample_days, 3)
        self.assertEqual(metric.confidence, "Low")
        self.assertEqual(metric.percentage_difference, -25.0)
        self.assertEqual(metric.trend_direction, IMPROVING)

    def test_single_day_history_does_not_require_previous_day(self):
        trends = HistoricalTrendsEngine().build(
            [row(date(2026, 7, 7), 9.0, 11.0)],
            as_of=date(2026, 7, 8),
        )

        metric = trends.cost.metrics["yesterday_cost"]
        self.assertEqual(metric.value, 9.0)
        self.assertIsNone(metric.comparison_value)
        self.assertIsNone(metric.difference_from_yesterday)
        self.assertIsNone(metric.percentage_difference)
        self.assertEqual(metric.trend_direction, UNCHANGED)

    def test_no_solar_returns_unavailable_solar_groups(self):
        history = [row(date(2026, 7, day), 8.0, 10.0) for day in range(1, 8)]

        trends = HistoricalTrendsEngine().build(history, as_of=date(2026, 7, 8))

        self.assertFalse(trends.solar.available)
        self.assertEqual(trends.solar.metrics, {})
        self.assertFalse(trends.self_consumption.available)
        self.assertEqual(trends.self_consumption.metrics, {})

    def test_no_exports_returns_unavailable_exports_but_keeps_solar(self):
        history = [row(date(2026, 7, day), 8.0, 10.0, solar=6.0, export=0.0) for day in range(1, 8)]

        trends = HistoricalTrendsEngine().build(history, as_of=date(2026, 7, 8))

        self.assertTrue(trends.solar.available)
        self.assertFalse(trends.exports.available)
        self.assertTrue(trends.self_consumption.available)
        self.assertAlmostEqual(trends.self_consumption.metrics["rolling_average_self_consumption"].value, 100.0)

    def test_missing_values_are_ignored_without_false_zeroes(self):
        history = [
            {"day": "2026-07-01", "net_cost": None, "grid_import_kwh": 8.0},
            {"day": "2026-07-02", "net_cost": "bad", "grid_import_kwh": None},
            {"day": "2026-07-07", "net_cost": 14.0, "grid_import_kwh": 12.0},
        ]

        trends = HistoricalTrendsEngine().build(history, as_of=date(2026, 7, 8))

        cost_metric = trends.cost.metrics["rolling_7_day_average_cost"]
        grid_metric = trends.grid.metrics["rolling_7_day_average_import"]
        self.assertEqual(cost_metric.value, 14.0)
        self.assertEqual(cost_metric.sample_days, 1)
        self.assertEqual(grid_metric.value, 10.0)
        self.assertEqual(grid_metric.sample_days, 2)

    def test_percentage_calculation_and_direction_are_domain_aware(self):
        history = [
            row(date(2026, 7, 6), 20.0, 20.0, solar=5.0, export=2.0),
            row(date(2026, 7, 7), 10.0, 30.0, solar=10.0, export=4.0),
        ]

        trends = HistoricalTrendsEngine().build(history, as_of=date(2026, 7, 8))

        cost = trends.cost.metrics["yesterday_cost"]
        grid = trends.grid.metrics["yesterday_grid_import"]
        solar = trends.solar.metrics["yesterday_generation"]
        self.assertEqual(cost.percentage_difference, -50.0)
        self.assertEqual(cost.trend_direction, IMPROVING)
        self.assertEqual(grid.percentage_difference, 50.0)
        self.assertEqual(grid.trend_direction, WORSENING)
        self.assertEqual(solar.percentage_difference, 100.0)
        self.assertEqual(solar.trend_direction, IMPROVING)

    def test_divide_by_zero_protection_returns_zero_percentage(self):
        history = [
            row(date(2026, 7, 6), 0.0, 0.0, solar=0.0, export=0.0),
            row(date(2026, 7, 7), 5.0, 4.0, solar=2.0, export=1.0),
        ]

        trends = HistoricalTrendsEngine().build(history, as_of=date(2026, 7, 8))

        self.assertEqual(trends.cost.metrics["yesterday_cost"].percentage_difference, 0.0)
        self.assertEqual(trends.grid.metrics["yesterday_grid_import"].percentage_difference, 0.0)

    def test_dataclass_daily_history_is_supported(self):
        @dataclass
        class Daily:
            day: date
            net_cost: float
            grid_import_kwh: float
            solar_generated_kwh: float
            solar_exported_kwh: float

        history = [
            Daily(date(2026, 7, 6), 11.0, 14.0, 4.0, 1.0),
            Daily(date(2026, 7, 7), 12.0, 13.0, 5.0, 2.0),
        ]

        trends = HistoricalTrendsEngine().build(history, as_of=date(2026, 7, 8))
        data = trends.as_dict()

        self.assertEqual(data["yesterday"], "2026-07-07")
        self.assertIn("cost", data)
        self.assertEqual(data["solar"]["metrics"]["yesterday_generation"]["value"], 5.0)


if __name__ == "__main__":
    unittest.main()
