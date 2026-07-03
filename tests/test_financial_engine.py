import unittest
from dataclasses import dataclass
from datetime import date

from engine.financial_engine import FinancialEngine
from engine.models import FinancialImpact


@dataclass
class Summary:
    start: date = date(2026, 6, 6)
    end: date = date(2026, 6, 27)
    supply_days: int = 22
    grid_import_kwh: float = 120.0
    import_cost: float = 44.0
    supply_cost: float = 28.6
    export_credit: float = 3.2
    net_cost: float = 69.4
    solar_exported_kwh: float = 160.0
    ev_charging_kwh: float = 50.0
    ev_charging_cost: float = 8.0
    average_daily_net_cost: float = 3.1545454545454543


@dataclass
class Settings:
    current_billing_period_start: date = date(2026, 6, 6)
    current_billing_period_end = None
    previous_billing_period_start: date = date(2026, 5, 6)
    previous_billing_period_end: date = date(2026, 6, 5)


class FinancialEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = FinancialEngine()

    def test_basic_bill_arithmetic(self):
        self.assertAlmostEqual(self.engine.calculate_import_cost(10.0, 0.54), 5.4)
        self.assertAlmostEqual(self.engine.calculate_export_credit(25.0, 0.02), 0.5)
        self.assertAlmostEqual(self.engine.calculate_net_bill(5.4, 1.2, 0.5), 6.1)
        self.assertAlmostEqual(self.engine.cost_of_ev_session(12.5, 0.19), 2.375)

    def test_project_bill_returns_impact_without_database_access(self):
        summary = Summary()
        projected, end, basis, impact = self.engine.project_bill(summary, Settings())

        self.assertEqual(end, date(2026, 7, 6))
        self.assertEqual(basis, "previous period length")
        self.assertAlmostEqual(projected, summary.average_daily_net_cost * 31)
        self.assertIsInstance(impact, FinancialImpact)
        self.assertAlmostEqual(impact.billing_period, projected)

    def test_compare_tariff_plans_matches_existing_report_contract(self):
        comparison = self.engine.compare_tariff_plans(
            Summary(),
            "synergy_ev_add_on",
            "previous_flat_or_home_plan",
            {
                "flat_import_rate": 0.30,
                "supply_charge_per_day": 1.292269,
                "export_credit_rate": 0.02,
            },
        )

        self.assertAlmostEqual(comparison["comparison_plan"]["grid_import_cost"], 36.0)
        self.assertAlmostEqual(comparison["comparison_plan"]["export_credit"], 3.2)
        self.assertAlmostEqual(comparison["comparison_plan"]["ev_charging_cost"], 15.0)
        self.assertAlmostEqual(comparison["savings"], comparison["comparison_plan"]["net_bill"] - 69.4)
        self.assertEqual(set(comparison), {
            "current_plan",
            "comparison_plan",
            "difference",
            "savings",
            "monthly_savings_estimate",
            "ev_charging_cost_difference",
            "household_usage_cost_difference",
            "solar_export_value_difference",
            "recommendation",
        })

    def test_bill_change_contributions_are_ranked_by_impact(self):
        settings = type(
            "Settings",
            (),
            {
                "import_rates": {"peak": 0.60, "off_peak": 0.30, "overnight": 0.20, "super_off_peak": 0.10},
                "ev_kwh_per_100km": 16.0,
                "previous_car_l_per_100km": 8.0,
                "petrol_price_per_litre": 2.0,
            },
        )()
        previous = {
            "net_cost": 120.0,
            "reading_days": 30,
            "grid_import_kwh": 200.0,
            "import_cost": 60.0,
            "supply_cost": 38.0,
            "export_credit": 10.0,
            "solar_generated_kwh": 220.0,
            "ev_charging_source": "ocular_export",
            "ev_charging_kwh": 20.0,
            "ev_charging_cost": 4.0,
            "actual_ev_charging_kwh": 20.0,
            "actual_ev_charging_cost": 4.0,
            "buckets": {
                "peak": {"cost": 20.0},
                "off_peak": {"cost": 15.0},
                "overnight": {"cost": 15.0},
                "super_off_peak": {"cost": 10.0},
            },
        }
        current = {
            "net_cost": 190.0,
            "reading_days": 30,
            "grid_import_kwh": 260.0,
            "import_cost": 115.0,
            "supply_cost": 40.0,
            "export_credit": 6.0,
            "solar_generated_kwh": 160.0,
            "ev_charging_source": "ocular_export",
            "ev_charging_kwh": 80.0,
            "ev_charging_cost": 36.0,
            "actual_ev_charging_kwh": 80.0,
            "actual_ev_charging_cost": 36.0,
            "buckets": {
                "peak": {"cost": 55.0},
                "off_peak": {"cost": 20.0},
                "overnight": {"cost": 25.0},
                "super_off_peak": {"cost": 15.0},
            },
        }

        explanation = self.engine.explain_bill_change(current, previous, settings)
        impacts = [abs(item["impact"]) for item in explanation["contributors"]]

        self.assertTrue(explanation["available"])
        self.assertEqual(impacts, sorted(impacts, reverse=True))
        self.assertEqual(explanation["contributors"][0]["label"], "Peak usage increased")
        self.assertIn("EV charging increased", [item["label"] for item in explanation["contributors"]])
        self.assertGreater(len(explanation["negative_changes"]), 0)
        self.assertIsNotNone(explanation["ev_explanation"])
        self.assertAlmostEqual(explanation["ev_explanation"]["cost_if_all_peak"], 48.0)
        self.assertAlmostEqual(explanation["ev_explanation"]["petrol_equivalent_cost"], 80.0)

    def test_bill_change_narrative_mentions_largest_reasons_and_uncertainty(self):
        settings = type(
            "Settings",
            (),
            {
                "import_rates": {"peak": 0.60, "off_peak": 0.30, "overnight": 0.20, "super_off_peak": 0.10},
                "ev_kwh_per_100km": None,
                "previous_car_l_per_100km": 8.0,
                "petrol_price_per_litre": 2.0,
            },
        )()
        previous = {
            "net_cost": 100.0,
            "reading_days": 30,
            "grid_import_kwh": 150.0,
            "import_cost": 50.0,
            "supply_cost": 35.0,
            "export_credit": 8.0,
            "solar_generated_kwh": 200.0,
            "ev_charging_source": "estimated",
            "ev_charging_cost": 0.0,
            "buckets": {"peak": {"cost": 10.0}, "off_peak": {"cost": 10.0}, "overnight": {"cost": 10.0}, "super_off_peak": {"cost": 20.0}},
        }
        current = {
            "net_cost": 145.0,
            "reading_days": 8,
            "grid_import_kwh": 180.0,
            "import_cost": 80.0,
            "supply_cost": 38.0,
            "export_credit": 3.0,
            "solar_generated_kwh": 120.0,
            "ev_charging_source": "estimated",
            "ev_charging_cost": 0.0,
            "buckets": {"peak": {"cost": 40.0}, "off_peak": {"cost": 10.0}, "overnight": {"cost": 10.0}, "super_off_peak": {"cost": 20.0}},
        }

        explanation = self.engine.explain_bill_change(current, previous, settings, current_bill=160.0, projected=True)

        self.assertIn("projected to be", explanation["narrative"])
        self.assertIn("peak usage increased", explanation["narrative"])
        self.assertIn("Medium confidence", explanation["narrative"])
        self.assertIsNone(explanation["ev_explanation"])

    def test_bill_change_handles_missing_history(self):
        explanation = self.engine.explain_bill_change({"net_cost": 50.0}, None, object())

        self.assertFalse(explanation["available"])
        self.assertIn("previous completed billing period is unavailable", explanation["narrative"])


if __name__ == "__main__":
    unittest.main()
