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


if __name__ == "__main__":
    unittest.main()
