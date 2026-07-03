import unittest
from datetime import date, timedelta
from types import SimpleNamespace

from engine.insights_engine import HouseholdInsightsEngine


def trend(day, *, solar=6.0, exported=2.0, cost=4.0, ev=0.0):
    return SimpleNamespace(
        day=day,
        solar_generated_kwh=solar,
        solar_exported_kwh=exported,
        net_cost=cost,
        ev_charging_kwh=ev,
        ev_charging_source="ocular_export" if ev else "estimated",
    )


def summary(**overrides):
    data = {
        "reading_days": 12,
        "actual_ev_tariff_breakdown": {
            "super_off_peak": {"kwh": 12.0},
            "overnight": {"kwh": 8.0},
            "off_peak": {"kwh": 0.0},
            "peak": {"kwh": 0.0},
        },
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class HouseholdInsightsEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = HouseholdInsightsEngine()
        self.start = date(2026, 6, 1)

    def test_generates_ranked_interesting_observations(self):
        trends = [
            trend(self.start + timedelta(days=index), solar=6.0, exported=2.0, cost=5.0)
            for index in range(7)
        ]
        trends.extend(
            [
                trend(self.start + timedelta(days=7), solar=12.0, exported=6.0, cost=2.8),
                trend(self.start + timedelta(days=8), solar=6.0, exported=2.0, cost=2.7),
                trend(self.start + timedelta(days=9), solar=6.0, exported=2.0, cost=2.6),
            ]
        )
        comparison = {
            "period_b": {"label": "Previous billing period"},
            "summary_b": {
                "net_cost": 140.0,
                "reading_days": 20,
                "buckets": {"peak": {"cost": 45.0}},
            },
            "deltas": {"peak_cost": -12.0},
        }

        insights = self.engine.build(
            trends=trends,
            current_summary=summary(reading_days=10),
            billing_summary=summary(),
            comparison=comparison,
            projected_bill=110.0,
            recommendations=[],
        )

        self.assertLessEqual(len(insights), 3)
        self.assertTrue(all(item.headline for item in insights))
        self.assertTrue(all(item.explanation for item in insights))
        self.assertTrue(all(item.comparison_period for item in insights))
        self.assertTrue(all(item.confidence for item in insights))
        self.assertTrue(all(item.supporting_values for item in insights))
        self.assertIn("projected bill", " ".join(item.headline.lower() for item in insights))

    def test_suppresses_uninteresting_history(self):
        trends = [
            trend(self.start + timedelta(days=index), solar=6.0, exported=2.0, cost=4.0)
            for index in range(8)
        ]

        insights = self.engine.build(
            trends=trends,
            current_summary=summary(reading_days=8),
            billing_summary=summary(actual_ev_tariff_breakdown={}),
            comparison=None,
            projected_bill=None,
            recommendations=[],
        )

        self.assertEqual(insights, [])

    def test_ev_tariff_observation_requires_measured_sessions(self):
        insights = self.engine.build(
            trends=[],
            current_summary=summary(),
            billing_summary=summary(),
            comparison=None,
            projected_bill=None,
            recommendations=[],
        )

        self.assertEqual(len(insights), 1)
        self.assertIn("EV charging", insights[0].headline)
        self.assertEqual(insights[0].supporting_values["Share"], "100%")


if __name__ == "__main__":
    unittest.main()
