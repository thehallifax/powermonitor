import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report


def summary() -> report.Summary:
    return report.Summary(
        start=date(2026, 7, 1),
        end=date(2026, 7, 1),
        supply_days=1,
        intervals=4,
        reading_days=1,
        grid_import_kwh=8.0,
        import_cost=3.0,
        supply_cost=1.0,
        total_cost=4.0,
        export_credit=0.2,
        net_cost=3.8,
        solar_generated_kwh=10.0,
        solar_used_kwh=5.0,
        solar_exported_kwh=5.0,
        self_consumption_pct=50.0,
        solar_export_pct=50.0,
        peak_cost_share_pct=60.0,
        ev_charging_kwh=2.0,
        ev_charging_cost=0.36,
        ev_charging_source="estimated",
        actual_ev_charging_kwh=0.0,
        actual_ev_charging_cost=0.0,
        actual_ev_tariff_breakdown=report.empty_ev_breakdown(),
        actual_ev_cost_partial=False,
        ev_session_analytics={"sessions": [], "tariff_usage": report.empty_ev_breakdown()},
        average_daily_grid_import_kwh=8.0,
        average_daily_solar_generation_kwh=10.0,
        average_daily_net_cost=3.8,
        buckets={
            "overnight": {"kwh": 1.0, "cost": 0.18},
            "off_peak": {"kwh": 2.0, "cost": 0.56},
            "super_off_peak": {"kwh": 0.0, "cost": 0.0},
            "peak": {"kwh": 5.0, "cost": 3.0},
        },
    )


def settings():
    return SimpleNamespace(
        current_plan="synergy_ev_add_on",
        retailer="Synergy",
        import_rates={
            "super_off_peak": 0.08,
            "overnight": 0.18,
            "off_peak": 0.28,
            "peak": 0.60,
        },
        export_credit_rate=0.02,
    )


class BatteryReportPresentationTests(unittest.TestCase):
    def test_scenario_analysis_replaces_fixed_battery_card_with_comparison_object(self):
        analysis = report.scenario_analysis(
            summary(),
            settings(),
            battery_intervals=[
                {"measurement_time": "2026-07-01T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 5.0, "grid_import_kwh": 0.0},
                {"measurement_time": "2026-07-01T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 5.0},
            ],
        )

        self.assertNotIn("battery_shift_peak_imports", [item["id"] for item in analysis["scenarios"]])
        self.assertIn("battery_comparison", analysis)
        self.assertEqual([item["capacity_kwh"] for item in analysis["battery_comparison"]["options"]], [5.0, 10.0, 15.0, 20.0])
        self.assertIn("tariff_version", analysis["battery_comparison"])
        self.assertEqual(analysis["battery_comparison"]["tariff_name"], "Synergy EV Add On")
        self.assertEqual(analysis["battery_comparison"]["tariff_effective_from"], "2026-07-01")

    def test_battery_comparison_renders_on_decisions_scenario_section(self):
        analysis = report.scenario_analysis(
            summary(),
            settings(),
            battery_intervals=[
                {"measurement_time": "2026-07-01T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 5.0, "grid_import_kwh": 0.0},
                {"measurement_time": "2026-07-01T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 5.0},
            ],
        )

        markup = report.scenario_analysis_html(analysis)

        self.assertIn("Home battery sizing", markup)
        self.assertIn("No battery", markup)
        self.assertIn("5 kWh usable", markup)
        self.assertIn("Solar export available", markup)
        self.assertIn("Non-cheapest import", markup)
        self.assertIn("Battery charged", markup)
        self.assertIn("Battery discharged", markup)
        self.assertIn("Recommended", markup)
        self.assertIn("Solar only", markup)
        self.assertIn("Solar + grid", markup)
        self.assertIn("Extra saving", markup)
        self.assertIn("New bill", markup)
        self.assertIn("scheduled grid charging", markup)
        self.assertIn("How was this calculated?", markup)
        self.assertNotIn("<th>Peak avoided</th>", markup.split("How was this calculated?", 1)[0])
        self.assertIn("Calculated using Synergy EV Add On tariff effective from 1 July 2026.", markup)

    def test_battery_comparison_renders_limited_benefit_and_oversized_signals(self):
        analysis = report.scenario_analysis(
            summary(),
            settings(),
            battery_intervals=[
                {"measurement_time": "2026-07-01T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 6.0, "grid_import_kwh": 0.0},
                {"measurement_time": "2026-07-01T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 5.0},
            ],
        )
        markup = report.scenario_analysis_html(analysis)

        self.assertIn("Limited additional benefit", markup)
        self.assertIn("Likely oversized for this usage pattern", markup)

    def test_battery_dispatch_diagnostics_view_renders_daily_validation_table(self):
        analysis = report.scenario_analysis(
            summary(),
            settings(),
            battery_intervals=[
                {"measurement_time": "2026-07-01T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 8.0, "grid_import_kwh": 0.0},
                {"measurement_time": "2026-07-01T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 3.0},
            ],
        )

        markup = report.battery_dispatch_diagnostics_html(analysis["battery_comparison"])

        self.assertIn("Battery dispatch diagnostics", markup)
        self.assertIn("Solar plus cheap-grid charging", markup)
        self.assertIn("Start SOC", markup)
        self.assertIn("Solar charged", markup)
        self.assertIn("Grid charged", markup)
        self.assertIn("Losses", markup)
        self.assertIn("Rejected", markup)
        self.assertIn("No battery dispatch invariant warnings", markup)

    def test_battery_comparison_empty_state_renders_without_fake_savings(self):
        analysis = report.scenario_analysis(summary(), settings(), battery_intervals=[])
        markup = report.scenario_analysis_html(analysis)

        self.assertIn("Battery modelling is unavailable", markup)
        self.assertIn("no interval data", markup)


if __name__ == "__main__":
    unittest.main()
