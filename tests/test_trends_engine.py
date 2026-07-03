import unittest
from datetime import date, timedelta
from types import SimpleNamespace

from engine.trends_engine import TrendsEngine


def summary(**overrides):
    data = {
        "reading_days": 14,
        "average_daily_net_cost": 4.0,
        "grid_import_kwh": 140.0,
        "solar_generated_kwh": 84.0,
        "solar_exported_kwh": 28.0,
        "self_consumption_pct": 66.7,
        "ev_charging_kwh": 20.0,
        "ev_charging_cost": 4.0,
        "buckets": {
            "peak": {"kwh": 14.0, "cost": 7.0},
            "overnight": {"kwh": 42.0, "cost": 8.0},
        },
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def trend(day, *, solar=6.0, exported=2.0, cost=4.0, grid=10.0, ev=0.0):
    return SimpleNamespace(
        day=day,
        solar_generated_kwh=solar,
        solar_exported_kwh=exported,
        net_cost=cost,
        grid_import_kwh=grid,
        ev_charging_kwh=ev,
    )


class TrendsEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = TrendsEngine()
        self.start = date(2026, 6, 1)

    def test_builds_long_term_trends_from_summary_comparison(self):
        current = summary(average_daily_net_cost=3.0, solar_generated_kwh=70.0, buckets={"peak": {"kwh": 8.0}, "overnight": {"kwh": 30.0}})
        previous = summary(average_daily_net_cost=4.0, solar_generated_kwh=100.0, buckets={"peak": {"kwh": 16.0}, "overnight": {"kwh": 40.0}})

        report = self.engine.build(
            current_summary=current,
            previous_summary=previous,
            current_trends=[],
            previous_trends=[],
            comparison_label="previous billing period",
        )

        labels = [trend.label for trend in report.trends]
        self.assertIn("Average daily cost", labels)
        self.assertIn("Peak usage", labels)
        cost = next(trend for trend in report.trends if trend.label == "Average daily cost")
        self.assertEqual(cost.direction, "down")
        self.assertAlmostEqual(cost.percentage_change, -25.0)
        self.assertIn("previous billing period", cost.explanation)

    def test_tracks_personal_records_with_previous_record(self):
        current = [
            trend(self.start + timedelta(days=0), solar=5.0, exported=1.0, cost=5.0),
            trend(self.start + timedelta(days=1), solar=12.0, exported=5.0, cost=2.5),
            trend(self.start + timedelta(days=2), solar=8.0, exported=7.0, cost=4.0, ev=9.0),
        ]
        previous = [
            trend(self.start - timedelta(days=3), solar=10.0, exported=4.0, cost=3.0),
            trend(self.start - timedelta(days=2), solar=7.0, exported=6.0, cost=3.5),
            trend(self.start - timedelta(days=1), solar=6.0, exported=2.0, cost=4.0),
        ]

        report = self.engine.build(
            current_summary=summary(),
            previous_summary=summary(),
            current_trends=current,
            previous_trends=previous,
            comparison_label="previous billing period",
        )

        records = {record.record_id: record for record in report.records}
        self.assertIn("highest_solar_generation_day", records)
        self.assertEqual(records["highest_solar_generation_day"].value, "12.0 kWh")
        self.assertEqual(records["highest_solar_generation_day"].previous_record, "10.0 kWh")
        self.assertIn("lowest_cost_day", records)

    def test_suppresses_topics_already_used_by_insights(self):
        insight = SimpleNamespace(topic="solar_generation")

        report = self.engine.build(
            current_summary=summary(solar_generated_kwh=50.0),
            previous_summary=summary(solar_generated_kwh=100.0),
            current_trends=[],
            previous_trends=[],
            comparison_label="previous billing period",
            insights=[insight],
        )

        self.assertNotIn("solar_generation", {trend.topic for trend in report.trends})

    def test_requires_enough_comparison_history_for_trends(self):
        report = self.engine.build(
            current_summary=summary(reading_days=2),
            previous_summary=summary(reading_days=14),
            current_trends=[],
            previous_trends=[],
        )

        self.assertEqual(report.trends, [])


if __name__ == "__main__":
    unittest.main()
