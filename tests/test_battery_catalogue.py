import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.battery_catalogue import (
    CATALOGUE_DISCLAIMER,
    capacity_fit_label,
    load_battery_catalogue,
    match_battery_products,
    product_checksum,
    validate_battery_catalogue,
)

COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))

import report


def product(**overrides):
    data = {
        "product_id": "test_modular",
        "manufacturer": "Example",
        "product_family": "Example Stack",
        "model_name": "Stack 5",
        "region": "AU",
        "status": "active",
        "source": "Manufacturer specification",
        "source_url": "https://example.com/spec",
        "source_checked_at": "2026-07-14",
        "data_confidence": "High",
        "chemistry": "LFP",
        "usable_capacity_kwh": 5.0,
        "nominal_capacity_kwh": None,
        "continuous_charge_power_kw": 5.0,
        "continuous_discharge_power_kw": 5.0,
        "peak_discharge_power_kw": None,
        "round_trip_efficiency_pct": 94.0,
        "warranty_years": 10,
        "warranty_cycles": None,
        "warranty_throughput_mwh": None,
        "minimum_state_of_charge_pct": None,
        "backup_capable": True,
        "blackout_startup_capable": None,
        "outdoor_installation_supported": True,
        "operating_temperature_notes": None,
        "modular": True,
        "module_capacity_kwh": 5.0,
        "minimum_modules": 1,
        "maximum_modules": 4,
        "supported_system_capacities_kwh": [5.0, 10.0, 15.0, 20.0],
        "integrated_inverter": False,
        "inverter_type": "Battery requiring compatible inverter",
        "compatible_inverter_families": [],
        "three_phase_support": None,
        "single_phase_support": None,
        "gateway_or_controller_required": "Controller requirements depend on installation.",
        "installation_notes": "Confirm installation.",
        "estimated_installed_cost_low": 10000,
        "estimated_installed_cost_high": 13000,
        "cost_currency": "AUD",
        "cost_region": "AU",
        "cost_checked_at": "2026-07-14",
        "rebate_eligible": True,
        "lifecycle_status": "active",
        "notes": "Test product.",
        "version": "2026-07-14",
    }
    data.update(overrides)
    data["checksum"] = product_checksum(data)
    return data


class BatteryCatalogueTests(unittest.TestCase):
    def test_catalogue_loading_and_validation(self):
        catalogue = load_battery_catalogue(PROJECT_ROOT / "data" / "catalogues" / "batteries")

        self.assertTrue(catalogue.valid)
        self.assertGreaterEqual(len(catalogue.products), 6)
        summary = catalogue.summary()
        self.assertEqual(summary["validation_status"], "valid")
        self.assertEqual(summary["region"], "AU")
        self.assertIn("solaredge_home_battery_400v_au", {item["product_id"] for item in catalogue.products})

    def test_validation_handles_malformed_and_duplicate_entries(self):
        first = product(product_id="duplicate")
        duplicate = product(product_id="duplicate", model_name="Other")
        malformed = {"product_id": "broken", "checksum": "not-a-checksum"}

        result = validate_battery_catalogue([first, duplicate, malformed])

        self.assertTrue(any("duplicate product_id" in item for item in result["errors"]))
        self.assertTrue(any("usable_capacity_kwh must be positive" in item for item in result["errors"]))
        self.assertTrue(any("checksum must be" in item for item in result["errors"]))

    def test_catalogue_cli_validation_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalogue.json"
            path.write_text(json.dumps({"catalogue_version": "test", "region": "AU", "checked_at": "2026-07-14", "products": [product()]}), encoding="utf-8")

            catalogue = load_battery_catalogue(Path(temporary))

        self.assertTrue(catalogue.valid)
        self.assertEqual(catalogue.summary()["product_count"], 1)

    def test_catalogue_cli_validation_command(self):
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "collector" / "validate_battery_catalogue.py")],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"validation_status": "valid"', result.stdout)

    def test_fixed_and_modular_matching_near_15_kwh(self):
        catalogue = load_battery_catalogue(PROJECT_ROOT / "data" / "catalogues" / "batteries")
        matches = match_battery_products(
            15.0,
            catalogue,
            household_context={"region": "AU", "inverter_brand": "SolarEdge", "inverter_model": "Home Hub"},
            investment={
                "annual_saving": 1188.0,
                "net_cost": 12500.0,
                "assumptions": {"government_rebate": 0.0, "annual_degradation_pct": 2.0, "electricity_inflation_pct": 3.0, "discount_rate_pct": 0.0},
                "options": [
                    {"capacity_kwh": 10.0, "annual_saving": 1000.0},
                    {"capacity_kwh": 15.0, "annual_saving": 1188.0},
                    {"capacity_kwh": 20.0, "annual_saving": 1273.0},
                ],
            },
        )

        ids = [item["product_id"] for item in matches["matches"]]
        self.assertIn("sigenergy_sigenstor_bat_8_au", ids)
        self.assertIn("tesla_powerwall_3_au", ids)
        self.assertIn("solaredge_home_battery_400v_au", ids)
        sigen = next(item for item in matches["matches"] if item["product_id"] == "sigenergy_sigenstor_bat_8_au")
        self.assertEqual(sigen["configured_capacity_kwh"], 15.6)
        self.assertEqual(sigen["module_configuration"]["modules"], 2)
        self.assertEqual(sigen["capacity_fit"], "Excellent fit")
        self.assertIsNotNone(sigen["estimated_payback_range"])
        self.assertIn("SolarEdge", " ".join(next(item for item in matches["matches"] if item["product_id"] == "solaredge_home_battery_400v_au")["compatibility_reasons"]))
        self.assertTrue(any(item["manufacturer"] != "SolarEdge" for item in matches["matches"]))
        self.assertEqual(matches["disclaimer"], CATALOGUE_DISCLAIMER)

    def test_capacity_fit_labels(self):
        self.assertEqual(capacity_fit_label(0.5, 3.3), "Excellent fit")
        self.assertEqual(capacity_fit_label(1.5, 10.0), "Close fit")
        self.assertEqual(capacity_fit_label(-4.0, -26.7), "Smaller than recommended")
        self.assertEqual(capacity_fit_label(8.0, 53.0), "Poor capacity fit")

    def test_missing_price_keeps_match_without_payback(self):
        catalogue = load_battery_catalogue(PROJECT_ROOT / "data" / "catalogues" / "batteries")
        matches = match_battery_products(10.0, catalogue, household_context={"region": "AU"})
        solaredge = next(item for item in matches["matches"] if item["product_id"] == "solaredge_home_battery_400v_au")

        self.assertIsNone(solaredge["cost_range"])
        self.assertIsNone(solaredge["estimated_payback_range"])
        self.assertTrue(any("Indicative installed cost" in warning for warning in solaredge["warnings"]))

    def test_report_presentation_is_neutral_and_mobile_safe(self):
        catalogue = load_battery_catalogue(PROJECT_ROOT / "data" / "catalogues" / "batteries")
        matches = match_battery_products(15.0, catalogue, household_context={"region": "AU"})

        markup = report.battery_product_matches_html(matches)
        css = (PROJECT_ROOT / "collector" / "report.py").read_text(encoding="utf-8")

        self.assertIn("Battery systems near your recommended size", markup)
        self.assertIn("Compare matched systems", markup)
        self.assertIn("Worth including", markup) if "Worth including" in markup else self.assertIn("Requires installer confirmation", markup)
        self.assertNotIn("Buy this battery", markup)
        self.assertNotIn("best brand", markup.lower())
        self.assertNotIn("Guaranteed compatible", markup)
        self.assertIn("battery-product-table-wrap", css)
        self.assertIn("overflow-x:auto", css)


if __name__ == "__main__":
    unittest.main()
