import sys
import unittest
from datetime import date, timedelta
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
        battery_installed_costs={"5": 6500.0, "10": 9500.0, "15": 12500.0, "20": 15500.0},
        battery_round_trip_efficiency=0.90,
        battery_life_years=15,
        battery_annual_degradation_pct=2.0,
        battery_electricity_inflation_pct=3.0,
        battery_discount_rate_pct=0.0,
        battery_government_rebate=0.0,
    )


def seasonal_days(start: date, count: int, solar_export: float = 12.0, grid_import: float = 12.0) -> list[dict]:
    rows = []
    for offset in range(count):
        day = (start + timedelta(days=offset)).isoformat()
        rows.extend(
            [
                {"measurement_time": f"{day}T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": solar_export, "grid_import_kwh": 0.0, "solar_generation_kwh": solar_export + 4.0, "household_consumption_kwh": 8.0, "source_resolution": "interval_15m"},
                {"measurement_time": f"{day}T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": grid_import, "solar_generation_kwh": 0.0, "household_consumption_kwh": grid_import, "source_resolution": "interval_15m"},
            ]
        )
    return rows


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
        self.assertIn("battery_investment", analysis)
        self.assertTrue(analysis["battery_investment"]["available"])
        for field in (
            "recommended_size",
            "installed_cost",
            "rebate",
            "net_cost",
            "annual_saving",
            "simple_payback_years",
            "ten_year_net",
            "fifteen_year_net",
            "confidence",
            "assumptions",
        ):
            self.assertIn(field, analysis["battery_investment"])
        self.assertEqual(analysis["battery_investment"]["recommended_size"], analysis["battery_comparison"]["recommended"]["capacity_kwh"])
        self.assertEqual([item["capacity_kwh"] for item in analysis["battery_comparison"]["options"]], [5.0, 10.0, 15.0, 20.0])
        self.assertIn("tariff_version", analysis["battery_comparison"])
        self.assertEqual(analysis["battery_comparison"]["tariff_name"], "Synergy EV Add On")
        self.assertEqual(analysis["battery_comparison"]["tariff_effective_from"], "2026-07-01")

    def test_battery_investment_calculates_payback_degradation_inflation_and_rebate(self):
        comparison = {
            "available": True,
            "recommended": {"capacity_kwh": 10.0},
            "strategies": {
                "solar_plus_grid_arbitrage": {
                    "options": [
                        {"capacity_kwh": 10.0, "label": "10 kWh usable", "annualised_savings": 1000.0},
                    ]
                }
            },
            "data_quality": {"days": 30},
        }
        custom_settings = SimpleNamespace(
            battery_installed_costs={"10": 9500.0},
            battery_round_trip_efficiency=0.90,
            battery_life_years=15,
            battery_annual_degradation_pct=2.0,
            battery_electricity_inflation_pct=3.0,
            battery_discount_rate_pct=0.0,
            battery_government_rebate=1000.0,
        )

        investment = report.battery_investment_analysis(comparison, custom_settings)

        self.assertEqual(investment["installed_cost"], 9500.0)
        self.assertEqual(investment["rebate"], 1000.0)
        self.assertEqual(investment["net_cost"], 8500.0)
        self.assertAlmostEqual(investment["simple_payback_years"], 8.5)
        expected_ten_year = sum(1000.0 * (0.98 ** year) * (1.03 ** year) for year in range(10))
        self.assertAlmostEqual(investment["ten_year_estimated_savings"], expected_ten_year)
        self.assertAlmostEqual(investment["ten_year_net"], expected_ten_year - 8500.0)
        self.assertEqual(investment["confidence"], "High")

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
        self.assertIn("Period solar + grid", markup)
        self.assertIn("Extra period saving", markup)
        self.assertIn("Period bill", markup)
        self.assertIn("scheduled grid charging", markup)
        self.assertIn("Why this size?", markup)
        self.assertIn("Maximum savings", markup)
        self.assertIn("Best balance of purchase cost and savings for current usage", markup)
        self.assertIn("Current billing-period evidence", markup)
        self.assertIn("Seasonal evidence", markup)
        self.assertIn("Methodology", markup)
        self.assertIn("<h4>Data used</h4>", markup)
        self.assertIn("<h4>Tariffs used</h4>", markup)
        self.assertIn("<h4>Battery dispatch assumptions</h4>", markup)
        self.assertIn("<h4>Grid charging assumptions</h4>", markup)
        self.assertIn("<h4>Limitations</h4>", markup)
        self.assertIn("Calculated using Synergy EV Add On tariff effective from 1 July 2026.", markup)
        self.assertIn('id="battery-investment"', markup)
        self.assertIn("Estimated investment", markup)
        self.assertIn("Estimated annual saving", markup)
        self.assertIn("Simple payback", markup)
        self.assertIn("10-year outcome", markup)
        self.assertIn("15-year outcome", markup)
        self.assertIn("Overall recommendation", markup)
        self.assertIn("Projection assumes 15-year battery life, 2% annual degradation, 3% electricity price growth and current usage patterns.", markup)

    def test_battery_investment_anchor_link_targets_section_without_broken_anchor(self):
        analysis = report.scenario_analysis(
            summary(),
            settings(),
            battery_intervals=[
                {"measurement_time": "2026-07-01T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 5.0, "grid_import_kwh": 0.0},
                {"measurement_time": "2026-07-01T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 5.0},
            ],
        )

        markup = report.scenario_analysis_html(analysis)
        report_source = (PROJECT_ROOT / "collector" / "report.py").read_text(encoding="utf-8")
        dashboard_js = (PROJECT_ROOT / "collector" / "assets" / "dashboard.js").read_text(encoding="utf-8")

        self.assertIn('<a href="#battery-investment">See estimated cost and payback</a>', markup)
        self.assertIn('id="battery-investment"', markup)
        self.assertNotIn('href="#home#battery-investment"', markup)
        self.assertNotIn('href="/#battery-investment"', markup)
        self.assertIn("body:has(#battery-investment:target) #whatif", report_source)
        self.assertIn('a[href^="#"]', dashboard_js)
        self.assertIn("scrollIntoView({ behavior: scrollBehavior", dashboard_js)

    def test_battery_comparison_renders_seasonal_model_when_history_exists(self):
        seasonal_rows = []
        for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
            seasonal_rows.extend(
                [
                    {"measurement_time": f"{day}T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 10.0, "grid_import_kwh": 0.0, "solar_generation_kwh": 14.0, "household_consumption_kwh": 8.0, "source_resolution": "interval_15m"},
                    {"measurement_time": f"{day}T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 10.0, "solar_generation_kwh": 0.0, "household_consumption_kwh": 10.0, "source_resolution": "interval_15m"},
                ]
            )
        for day in ("2026-07-01", "2026-07-02", "2026-07-03"):
            seasonal_rows.extend(
                [
                    {"measurement_time": f"{day}T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 5.0, "grid_import_kwh": 0.0, "solar_generation_kwh": 8.0, "household_consumption_kwh": 5.0, "source_resolution": "interval_15m"},
                    {"measurement_time": f"{day}T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 5.0, "solar_generation_kwh": 0.0, "household_consumption_kwh": 5.0, "source_resolution": "interval_15m"},
                ]
            )

        analysis = report.scenario_analysis(
            summary(),
            settings(),
            battery_intervals=[
                {"measurement_time": "2026-07-01T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 5.0, "grid_import_kwh": 0.0},
                {"measurement_time": "2026-07-01T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 5.0},
            ],
            seasonal_battery_intervals=seasonal_rows,
        )
        markup = report.scenario_analysis_html(analysis)

        self.assertTrue(analysis["battery_comparison"]["seasonal_model"]["available"])
        self.assertIn("confidence_model", analysis["battery_comparison"])
        self.assertIn("Seasonal battery model", markup)
        self.assertIn("Summer", markup)
        self.assertIn("Winter", markup)
        self.assertIn("Annual weighting", markup)
        self.assertEqual(analysis["battery_recommendation"]["source"], "current_period")
        self.assertEqual(analysis["battery_investment"]["recommended_size"], analysis["battery_comparison"]["recommended"]["capacity_kwh"])

    def test_high_confidence_annual_history_drives_primary_presentation_api_and_investment(self):
        seasonal_rows = []
        seasonal_rows.extend(seasonal_days(date(2026, 1, 1), 40))
        seasonal_rows.extend(seasonal_days(date(2026, 4, 1), 40))
        seasonal_rows.extend(seasonal_days(date(2026, 7, 1), 40))

        analysis = report.scenario_analysis(
            summary(),
            settings(),
            battery_intervals=[
                {"measurement_time": "2026-07-01T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 5.0, "grid_import_kwh": 0.0},
                {"measurement_time": "2026-07-01T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 5.0},
            ],
            seasonal_battery_intervals=seasonal_rows,
        )
        markup = report.scenario_analysis_html(analysis)
        recommendation = analysis["battery_recommendation"]
        annual_size = analysis["battery_comparison"]["seasonal_model"]["annual_recommendation"]["capacity_kwh"]

        self.assertEqual(recommendation["source"], "annual_historical")
        self.assertEqual(recommendation["recommended_size_kwh"], annual_size)
        self.assertEqual(analysis["battery_investment"]["recommended_size"], annual_size)
        self.assertEqual(analysis["battery_investment"]["recommendation_source"], "annual_historical")
        self.assertEqual(analysis["battery_investment"]["interval_days"], 120)
        self.assertEqual(analysis["battery_investment"]["seasonal_confidence"], "High")
        self.assertIn("Recommended battery", markup)
        self.assertIn("Annual history", markup)
        self.assertIn("Estimated annual bill reduction", markup)
        self.assertLess(markup.index("Recommended battery"), markup.index("Why this size?"))
        self.assertIn("Why this size?", markup)
        self.assertIn("Budget option", markup)
        self.assertIn("Recommended", markup)
        self.assertIn("Maximum savings", markup)
        self.assertIn("This recommendation is based on your current household usage.", markup)
        self.assertIn("Current billing-period result", markup)
        self.assertIn("Best for selected period", markup)
        self.assertIn("Annual values combine included seasons", markup)
        self.assertIn("Period solar only", markup)
        self.assertIn("Annual bill reduction", markup)
        self.assertIn("Extra benefit versus previous size", markup)
        self.assertIn("Benefit versus recommended", markup)
        annual_table = markup.split("battery-annual-table", 1)[1].split("</table>", 1)[0]
        self.assertNotIn("Marginal gain", annual_table)
        self.assertNotIn("Next-size gain", annual_table)
        self.assertIn('<details class="battery-accordion">\n        <summary><span>Current billing-period evidence</span>', markup)
        self.assertIn('<details class="battery-accordion">\n        <summary><span>Seasonal evidence</span>', markup)
        current_section = markup.split("Current billing-period evidence", 1)[1].split("Seasonal evidence", 1)[0]
        self.assertNotIn("battery-signal recommended", current_section)
        self.assertIn("battery-table-wrap", markup)
        self.assertIn("battery-seasonal-table-wrap", markup)
        self.assertNotIn("<th>Solar only</th>", markup)

    def test_battery_purchase_decision_context_compares_neighbouring_sizes_and_upgrade_payback(self):
        comparison = {
            "available": True,
            "recommended": {"capacity_kwh": 15.0},
            "battery_recommendation": {"recommended_size_kwh": 15.0, "source": "current_period"},
            "strategies": {
                "solar_plus_grid_arbitrage": {
                    "options": [
                        {"capacity_kwh": 10.0, "label": "10 kWh usable", "annualised_savings": 1000.0},
                        {"capacity_kwh": 15.0, "label": "15 kWh usable", "annualised_savings": 1188.0},
                        {"capacity_kwh": 20.0, "label": "20 kWh usable", "annualised_savings": 1273.0},
                    ]
                }
            },
            "data_quality": {"days": 30},
        }
        custom_settings = SimpleNamespace(
            battery_installed_costs={"10": 9500.0, "15": 12000.0, "20": 15500.0},
            battery_round_trip_efficiency=0.90,
            battery_life_years=15,
            battery_annual_degradation_pct=2.0,
            battery_electricity_inflation_pct=3.0,
            battery_discount_rate_pct=0.0,
            battery_government_rebate=0.0,
        )
        investment = report.battery_investment_analysis(comparison, custom_settings)

        context = report.battery_purchase_decision_context(comparison, investment)
        markup = report.battery_comparison_html(comparison, investment, show_future_usage_note=True)

        self.assertEqual(context["smaller"]["capacity_kwh"], 10.0)
        self.assertEqual(context["larger"]["capacity_kwh"], 20.0)
        self.assertEqual(context["smaller_delta"], 188.0)
        self.assertEqual(context["larger_delta"], 85.0)
        self.assertEqual(context["smaller_energy_delta"], 5.0)
        self.assertEqual(context["larger_energy_delta"], 5.0)
        self.assertAlmostEqual(context["larger_percentage_improvement"], 85.0 / 1188.0 * 100)
        self.assertEqual(context["diminishing_return"], 103.0)
        self.assertEqual(context["budget_economics"]["lower_cost"], 2500.0)
        self.assertEqual(context["budget_economics"]["annual_saving_given_up"], 188.0)
        self.assertEqual(context["upgrade_economics"]["additional_cost"], 3500.0)
        self.assertEqual(context["upgrade_economics"]["additional_annual_saving"], 85.0)
        self.assertAlmostEqual(context["upgrade_economics"]["additional_payback_years"], 3500.0 / 85.0)
        self.assertIn("Budget option", markup)
        self.assertIn("Estimated annual savings: $1,000.00", markup)
        self.assertIn("Gives up around $188.00/year compared with the recommendation", markup)
        self.assertIn("Estimated lower purchase cost: $2,500.00", markup)
        self.assertIn("Low additional return", markup)
        self.assertIn("Maximum savings", markup)
        self.assertIn("Additional annual savings: $85.00", markup)
        self.assertIn("Estimated additional purchase cost: $3,500.00", markup)
        self.assertIn("Estimated extra payback: 41.2 years", markup)
        self.assertIn("Extra capacity is unlikely to repay itself from current usage", markup)
        self.assertIn("Moving from 10 kWh to 15 kWh increases estimated annual savings by around $188.00.", markup)
        self.assertIn("Moving from 15 kWh to 20 kWh only increases savings by around $85.00 per year.", markup)
        self.assertIn("Most of the available benefit is captured by the recommended size.", markup)
        self.assertIn("Choosing 20 kWh instead of 15 kWh", markup)
        self.assertIn("Additional investment", markup)
        self.assertIn("Additional annual saving", markup)
        self.assertIn("Extra payback", markup)
        self.assertIn("41.2 years", markup)
        self.assertIn("Future EVs, pool pumps, electric heating/cooling or household growth may justify a larger battery.", markup)
        self.assertEqual(report.battery_value_label(6.0, 15, 100.0), "Excellent value")
        self.assertEqual(report.battery_value_label(9.0, 15, 100.0), "Good value")
        self.assertEqual(
            report.battery_value_label(20.0, 15, 100.0),
            "Consider only if future electricity usage is expected to increase",
        )

    def test_battery_purchase_decision_omits_upgrade_economics_without_pricing(self):
        comparison = {
            "available": True,
            "recommended": {"capacity_kwh": 15.0},
            "battery_recommendation": {"recommended_size_kwh": 15.0, "source": "current_period"},
            "strategies": {
                "solar_plus_grid_arbitrage": {
                    "options": [
                        {"capacity_kwh": 10.0, "label": "10 kWh usable", "annualised_savings": 1000.0},
                        {"capacity_kwh": 15.0, "label": "15 kWh usable", "annualised_savings": 1188.0},
                        {"capacity_kwh": 20.0, "label": "20 kWh usable", "annualised_savings": 1273.0},
                    ]
                }
            },
            "data_quality": {"days": 30},
        }
        no_pricing_settings = SimpleNamespace(
            battery_installed_costs={},
            battery_round_trip_efficiency=0.90,
            battery_life_years=15,
            battery_annual_degradation_pct=2.0,
            battery_electricity_inflation_pct=3.0,
            battery_discount_rate_pct=0.0,
            battery_government_rebate=0.0,
        )
        investment = report.battery_investment_analysis(comparison, no_pricing_settings)

        context = report.battery_purchase_decision_context(comparison, investment)
        markup = report.battery_comparison_html(comparison, investment)

        self.assertIsNone(context["upgrade_economics"])
        self.assertIsNone(context["budget_economics"])
        self.assertIn("Maximum savings", markup)
        self.assertIn("Additional annual savings: $85.00", markup)
        self.assertNotIn("Estimated upgrade economics", markup)
        self.assertNotIn("Estimated additional purchase cost", markup)

    def test_battery_future_growth_note_uses_household_configuration(self):
        base_summary = summary()
        base_settings = settings()
        analysis = report.scenario_analysis(
            base_summary,
            base_settings,
            battery_intervals=[
                {"measurement_time": "2026-07-01T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 5.0, "grid_import_kwh": 0.0},
                {"measurement_time": "2026-07-01T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 5.0},
            ],
        )
        markup = report.scenario_analysis_html(analysis)

        self.assertTrue(analysis["battery_future_growth_note"])
        self.assertIn("Future EVs, pool pumps, electric heating/cooling or household growth may justify a larger battery.", markup)
        self.assertNotIn(
            "Future EVs, pool pumps, electric heating/cooling or household growth may justify a larger battery.",
            report.battery_comparison_html(analysis["battery_comparison"], analysis["battery_investment"], show_future_usage_note=False),
        )

    def test_battery_purchase_decision_rendering_does_not_change_sizing_or_api_shape(self):
        analysis = report.scenario_analysis(
            summary(),
            settings(),
            battery_intervals=[
                {"measurement_time": "2026-07-01T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 5.0, "grid_import_kwh": 0.0},
                {"measurement_time": "2026-07-01T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 5.0},
            ],
        )
        before_options = [dict(item) for item in analysis["battery_comparison"]["options"]]
        before_recommendation = dict(analysis["battery_recommendation"])

        report.scenario_analysis_html(analysis)

        self.assertEqual(analysis["battery_comparison"]["options"], before_options)
        self.assertEqual(analysis["battery_recommendation"], before_recommendation)
        for field in (
            "recommended_size",
            "installed_cost",
            "rebate",
            "net_cost",
            "annual_saving",
            "simple_payback_years",
            "ten_year_net",
            "fifteen_year_net",
            "confidence",
            "assumptions",
        ):
            self.assertIn(field, analysis["battery_investment"])
        self.assertIn("battery_future_growth_note", analysis)

    def test_battery_cards_keep_responsive_grid_rules(self):
        css = (PROJECT_ROOT / "collector" / "report.py").read_text()

        self.assertIn(".battery-tradeoff-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr));", css)
        self.assertIn(".battery-tradeoff-card {{ display:grid;", css)
        self.assertIn(".battery-tradeoff-grid", css.split("@media (max-width:900px)", 1)[1])

    def test_mixed_tariff_explanation_renders_in_battery_summary(self):
        seasonal_rows = []
        seasonal_rows.extend(seasonal_days(date(2026, 1, 1), 40))
        seasonal_rows.extend(seasonal_days(date(2026, 4, 1), 40))
        seasonal_rows.extend(seasonal_days(date(2026, 7, 1), 40))
        analysis = report.scenario_analysis(
            summary(),
            settings(),
            battery_intervals=[
                {"measurement_time": "2026-07-01T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 5.0, "grid_import_kwh": 0.0},
                {"measurement_time": "2026-07-01T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 5.0},
            ],
            seasonal_battery_intervals=seasonal_rows,
        )
        periods = [
            {"tariff_name": "Flat-rate tariff", "interval_count": 48},
            {"tariff_name": "Synergy EV Add On", "interval_count": 72},
        ]
        analysis["battery_comparison"]["tariff_periods_used"] = periods
        analysis["battery_comparison"]["tariff_history_warning"] = "This recommendation includes the flat-rate tariff through 30 April 2026 and the Synergy EV Add On from 1 May 2026."
        analysis["battery_comparison"]["battery_recommendation"]["tariff_periods_used"] = periods

        markup = report.battery_comparison_html(analysis["battery_comparison"])

        self.assertIn("flat-rate tariff through 30 April 2026", markup)
        self.assertIn("Historical tariff changes included", markup)
        self.assertIn("Current and previous household tariff periods", markup)
        self.assertIn("Cheap-grid battery charging is only modelled during periods where the active tariff includes a suitable cheap window.", markup)

    def test_annual_seasonal_mismatch_is_explained_in_homeowner_wording(self):
        seasonal_rows = []
        for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
            seasonal_rows.extend(seasonal_days(date.fromisoformat(day), 1, solar_export=10.0, grid_import=12.0))
        for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05", "2026-07-06"):
            seasonal_rows.extend(seasonal_days(date.fromisoformat(day), 1, solar_export=10.0, grid_import=12.0))

        analysis = report.scenario_analysis(
            summary(),
            settings(),
            battery_intervals=[
                {"measurement_time": "2026-07-01T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 6.0, "grid_import_kwh": 0.0},
                {"measurement_time": "2026-07-01T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 6.0},
            ],
            seasonal_battery_intervals=seasonal_rows,
        )
        mismatch = analysis["battery_comparison"]["seasonal_model"]["recommendation_mismatch"]
        markup = report.scenario_analysis_html(analysis)

        self.assertTrue(mismatch["exists"])
        self.assertIn("combined annual model", mismatch["reason"])
        self.assertIn("combined annual model", markup)

    def test_battery_comparison_hides_oversized_wording_without_high_confidence(self):
        analysis = report.scenario_analysis(
            summary(),
            settings(),
            battery_intervals=[
                {"measurement_time": "2026-07-01T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 6.0, "grid_import_kwh": 0.0},
                {"measurement_time": "2026-07-01T18:00:00", "tariff_bucket": "peak", "solar_export_kwh": 0.0, "grid_import_kwh": 5.0},
            ],
        )
        markup = report.scenario_analysis_html(analysis)

        self.assertIn("Limited extra value", markup)
        self.assertIn("Low additional return", markup)
        self.assertNotIn("Likely oversized for this usage pattern", markup)
        self.assertNotIn("Likely oversized", markup)

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
