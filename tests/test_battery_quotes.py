import json
import tempfile
import unittest
from pathlib import Path

from engine.battery_catalogue import load_battery_catalogue
from engine.battery_quotes import (
    BADGE_BEST_OVERALL,
    BADGE_REQUIRES_CLARIFICATION,
    BADGE_SINGLE_QUOTE,
    BatteryQuote,
    BatteryQuoteStore,
    compare_battery_quotes,
    normalize_quote_payload,
)
from engine.models import to_api_safe

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report


def investment_fixture() -> dict:
    return {
        "available": True,
        "recommended_size": 15.0,
        "annual_saving": 1200.0,
        "assumptions": {
            "annual_degradation_pct": 2.0,
            "electricity_inflation_pct": 3.0,
            "discount_rate_pct": 0.0,
        },
        "options": [
            {"capacity_kwh": 10.0, "annual_saving": 1000.0},
            {"capacity_kwh": 15.0, "annual_saving": 1200.0},
            {"capacity_kwh": 20.0, "annual_saving": 1275.0},
        ],
    }


def quote(**overrides) -> BatteryQuote:
    data = {
        "quote_id": "quote_one",
        "installer_name": "Installer One",
        "quote_date": "2026-07-14",
        "product_id": "sigenergy_sigenstor_bat_8_au",
        "configured_modules": 2,
        "configured_capacity": 15.6,
        "backup_included": True,
        "inverter_included": True,
        "backup_gateway_included": True,
        "installation_scope": "Battery installed beside switchboard.",
        "monitoring_included": True,
        "warranty_notes": "10 year manufacturer warranty.",
        "quoted_price": 14500.0,
        "rebate": 1500.0,
        "finance_cost": 0.0,
        "quote_status": "active",
        "homeowner_notes": "",
    }
    data.update(overrides)
    return BatteryQuote.from_dict(data)


class BatteryQuoteTests(unittest.TestCase):
    def test_quote_store_crud_duplicate_delete_and_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BatteryQuoteStore(Path(directory) / "battery_quotes.json")
            store.upsert(quote(quote_id="quote_1"))
            store.upsert(quote(quote_id="quote_1", installer_name="Edited installer"))
            self.assertEqual(len(store.load()), 1)
            self.assertEqual(store.load()[0].installer_name, "Edited installer")

            duplicate = store.duplicate("quote_1")
            self.assertNotEqual(duplicate.quote_id, "quote_1")
            self.assertEqual(len(store.load()), 2)

            store.delete(duplicate.quote_id)
            self.assertEqual([item.quote_id for item in store.load()], ["quote_1"])

            for index in range(2, 6):
                store.upsert(quote(quote_id=f"quote_{index}", installer_name=f"Installer {index}"))
            with self.assertRaises(ValueError):
                store.upsert(quote(quote_id="quote_6"))

    def test_modular_quote_capacity_is_calculated_from_module_count(self):
        catalogue = load_battery_catalogue()
        normalized = normalize_quote_payload(
            {
                "product_id": "sigenergy_sigenstor_bat_8_au",
                "configured_modules": "2",
                "configured_capacity": "99",
                "quoted_price": "14000",
            },
            catalogue,
        )

        self.assertEqual(normalized.configured_modules, 2)
        self.assertEqual(normalized.configured_capacity, 15.6)

    def test_quote_comparison_calculates_payback_benefits_and_badges(self):
        catalogue = load_battery_catalogue()
        comparisons = compare_battery_quotes(
            [
                quote(quote_id="quote_value", installer_name="Value Solar", quoted_price=13500.0, rebate=1500.0),
                quote(quote_id="quote_low", installer_name="Budget Solar", product_id="tesla_powerwall_3_au", configured_modules=None, configured_capacity=13.5, quoted_price=11000.0, rebate=0.0),
                quote(quote_id="quote_unclear", installer_name="Unclear Solar", product_id="", quoted_price=None, rebate=None),
            ],
            catalogue,
            15.0,
            investment_fixture(),
        )

        self.assertEqual(comparisons["active_quote_count"], 3)
        by_id = {item["quote_id"]: item for item in comparisons["comparisons"]}
        self.assertAlmostEqual(by_id["quote_value"]["usable_capacity"], 15.6)
        self.assertAlmostEqual(by_id["quote_value"]["net_price"], 12000.0)
        self.assertAlmostEqual(by_id["quote_value"]["estimated_annual_bill_reduction"], 1200.0)
        self.assertAlmostEqual(by_id["quote_value"]["simple_payback_years"], 10.0)
        self.assertGreater(by_id["quote_value"]["fifteen_year_net_benefit"], 0.0)
        self.assertEqual(by_id["quote_unclear"]["primary_badge"], BADGE_REQUIRES_CLARIFICATION)
        self.assertTrue(any(item["primary_badge"] == BADGE_BEST_OVERALL for item in comparisons["comparisons"]))
        self.assertTrue(all(item["primary_badge"] for item in comparisons["comparisons"]))
        self.assertTrue(comparisons["comparison_ready"])
        self.assertIn("currently offers the best overall value", comparisons["summary"])
        self.assertIn("primary_verdict", by_id["quote_value"])
        self.assertIn("verdict_explanation", by_id["quote_value"])

    def test_missing_information_warns_without_penalising_calculations(self):
        catalogue = load_battery_catalogue()
        result = compare_battery_quotes(
            [quote(inverter_included=False, backup_gateway_included=False, warranty_notes="", rebate=None)],
            catalogue,
            15.0,
            investment_fixture(),
        )
        comparison = result["comparisons"][0]

        self.assertIn("Confirm whether any required inverter is included.", comparison["confirmation_questions"])
        self.assertIn("Confirm the battery warranty and installation workmanship warranty.", comparison["confirmation_questions"])
        self.assertAlmostEqual(comparison["estimated_annual_bill_reduction"], 1200.0)
        self.assertIsNotNone(comparison["simple_payback_years"])

    def test_single_quote_uses_neutral_verdict_and_prompts_for_another_quote(self):
        result = compare_battery_quotes([quote()], load_battery_catalogue(), 15.0, investment_fixture())
        comparison = result["comparisons"][0]

        self.assertFalse(result["comparison_ready"])
        self.assertNotEqual(comparison["primary_verdict"], BADGE_BEST_OVERALL)
        self.assertIn(comparison["primary_verdict"], {"Matches recommendation", BADGE_SINGLE_QUOTE})
        self.assertIn("Add another quote to compare value", result["summary"])

    def test_quote_payload_is_api_safe_and_does_not_mutate_recommendation_outputs(self):
        catalogue = load_battery_catalogue()
        investment = investment_fixture()
        before = json.loads(json.dumps(investment, sort_keys=True))

        result = compare_battery_quotes([quote()], catalogue, 15.0, investment)
        encoded = json.dumps(to_api_safe({"battery_quotes": result["quotes"], "battery_quote_comparison": result}), sort_keys=True)

        self.assertIn("battery_quote_comparison", encoded)
        self.assertEqual(investment, before)

    def test_quote_rendering_has_controls_table_responsive_css_and_mobile_rule(self):
        result = compare_battery_quotes([quote()], load_battery_catalogue(), 15.0, investment_fixture())
        markup = report.battery_quote_comparison_html(result)
        css = (PROJECT_ROOT / "collector" / "report.py").read_text(encoding="utf-8")

        self.assertIn('id="battery-quotes"', markup)
        self.assertIn("Compare installer quotes", markup)
        self.assertIn("battery-quote-card", markup)
        self.assertIn("Estimated annual saving", markup)
        self.assertIn("Why PowerMonitor reached this result", markup)
        self.assertIn("Duplicate quote", markup)
        self.assertIn("Delete quote", markup)
        self.assertIn('action="/battery-quotes/save"', markup)
        self.assertIn("Compare quote details", markup)
        self.assertIn("More quote details", markup)
        self.assertIn("Quote PDF upload is planned for a future version.", markup)
        self.assertNotIn("investment_score", markup)
        self.assertNotIn("recommendation_score", markup)
        self.assertNotIn("★★★★★", markup)
        self.assertIn(".battery-quote-form {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr));", css)
        self.assertIn(".battery-quote-form", css.split("@media (max-width:900px)", 1)[1])

    def test_quote_rendering_handles_absent_pricing_gracefully(self):
        result = compare_battery_quotes(
            [quote(quoted_price=None, rebate=None, finance_cost=None)],
            load_battery_catalogue(),
            15.0,
            investment_fixture(),
        )
        markup = report.battery_quote_comparison_html(result)

        self.assertIn("Confirm with installer", markup)
        self.assertIn("Requires clarification", markup)

    def test_simplified_form_hides_advanced_fields_and_preserves_old_values(self):
        item = quote(homeowner_notes="Old homeowner note").as_dict()
        item.update({"usable_capacity": 15.6})
        products = compare_battery_quotes([], load_battery_catalogue(), 15.0, investment_fixture())["catalogue_products"]
        markup = report.battery_quote_form_html(item, products, "Save quote")

        self.assertIn("Total quoted price", markup)
        self.assertIn("Enter the total installed price shown on the quote.", markup)
        self.assertIn("Enter the rebate separately only if it is not already included", markup)
        self.assertIn("More quote details", markup)
        self.assertIn("Finance cost", markup)
        self.assertIn("Warranty notes", markup)
        self.assertIn('name="homeowner_notes" value="Old homeowner note"', markup)

    def test_homeowner_friendly_product_labels_and_modular_capacity_display(self):
        result = compare_battery_quotes([], load_battery_catalogue(), 15.0, investment_fixture())
        labels = [item["label"] for item in result["catalogue_products"]]

        self.assertTrue(any("Tesla Powerwall 3 - 13.5 kWh" in label for label in labels))
        self.assertTrue(any("Sigenergy SigenStor - modular" in label for label in labels))
        self.assertTrue(any("kWh per module" in label for label in labels))

        markup = report.battery_quote_form_html({}, result["catalogue_products"], "Add quote")
        self.assertIn("data-module-size", markup)
        self.assertIn("Configured usable capacity", markup)
        self.assertIn("data-capacity-display", markup)


if __name__ == "__main__":
    unittest.main()
