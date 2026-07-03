import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import web
from engine.tariff_catalog import SUPPORTED_PLANS


class WebConfigTests(unittest.TestCase):
    def test_default_bind_is_lan_reachable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_settings = Path(temp_dir) / "missing.json"
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    web.resolve_web_bind(missing_settings),
                    ("0.0.0.0", 8765),
                )

    def test_config_file_can_set_host_and_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            settings.write_text(
                json.dumps({"web_server": {"host": "0.0.0.0", "port": 9876}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(web.resolve_web_bind(settings), ("0.0.0.0", 9876))

    def test_legacy_flat_config_keys_are_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            settings.write_text(
                json.dumps({"web_host": "0.0.0.0", "web_port": "9877"}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(web.resolve_web_bind(settings), ("0.0.0.0", 9877))

    def test_environment_overrides_config_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            settings.write_text(
                json.dumps({"web_server": {"host": "127.0.0.1", "port": 8765}}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"POWMONITOR_HOST": "0.0.0.0", "POWMONITOR_PORT": "9999"},
                clear=True,
            ):
                self.assertEqual(web.resolve_web_bind(settings), ("0.0.0.0", 9999))

    def test_localhost_only_mode_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            settings.write_text(
                json.dumps({"web_server": {"host": "127.0.0.1", "port": 8765}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(web.resolve_web_bind(settings), ("127.0.0.1", 8765))

    def test_invalid_port_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            settings.write_text(
                json.dumps({"web_server": {"host": "127.0.0.1", "port": "nope"}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError):
                    web.resolve_web_bind(settings)

    def test_billing_cycle_settings_update_non_secret_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            settings.write_text(
                json.dumps({"current_plan": "synergy_ev_add_on", "import_rates": {"peak": 0.54}}),
                encoding="utf-8",
            )

            updated = web.update_billing_cycle_settings(
                settings,
                billing_period_start_day="12",
                state_or_territory="WA",
                retailer="Synergy",
                current_plan="synergy_home_plan",
            )
            saved = json.loads(settings.read_text(encoding="utf-8"))

        self.assertEqual(updated["billing_period_start_day"], 12)
        self.assertTrue(saved["billing_cycle_configured"])
        self.assertEqual(saved["retailer"], "Synergy")
        self.assertEqual(saved["state_or_territory"], "WA")
        self.assertEqual(saved["tariff_setup_mode"], "catalog")
        self.assertEqual(saved["current_plan"], "synergy_home_plan")
        self.assertEqual(saved["import_rates"]["peak"], 0.327582)
        self.assertEqual(updated["comparison_plan_rates"]["flat_import_rate"], 0.30)

    def test_wa_synergy_ev_add_on_selection_applies_catalog_rates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"

            updated = web.update_billing_cycle_settings(
                settings,
                billing_period_start_day="6",
                state_or_territory="WA",
                retailer="synergy",
                current_plan="synergy_ev_add_on",
            )

        self.assertEqual(updated["retailer"], "Synergy")
        self.assertEqual(updated["retailer_id"], "synergy")
        self.assertEqual(updated["current_plan"], "synergy_ev_add_on")
        self.assertEqual(updated["tariff_add_on"], "none")
        self.assertAlmostEqual(updated["import_rates"]["peak"], 0.538446)
        self.assertAlmostEqual(updated["import_rates"]["super_off_peak"], 0.086151)

    def test_ev_add_on_is_not_applied_as_separate_add_on(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"

            updated = web.update_billing_cycle_settings(
                settings,
                billing_period_start_day="6",
                state_or_territory="WA",
                retailer="synergy",
                current_plan="synergy_home_plan",
                tariff_add_on="ev_add_on",
            )

        self.assertEqual(updated["current_plan"], "synergy_home_plan")
        self.assertEqual(updated["tariff_add_on"], "none")
        self.assertAlmostEqual(updated["import_rates"]["peak"], 0.327582)

    def test_catalogue_plans_have_distinct_rates_and_windows(self):
        home = SUPPORTED_PLANS["synergy_home_plan"]
        ev = SUPPORTED_PLANS["synergy_ev_add_on"]
        previous = SUPPORTED_PLANS["previous_flat_or_home_plan"]

        self.assertNotEqual(home.import_rates, ev.import_rates)
        self.assertNotEqual(home.import_rates, previous.import_rates)
        self.assertGreater(ev.import_rates["peak"], ev.import_rates["super_off_peak"])
        self.assertEqual(len(set(home.import_rates.values())), 1)
        self.assertEqual(len(set(previous.import_rates.values())), 1)
        self.assertIn("9am-3pm", " ".join(ev.tariff_windows))
        self.assertIn("Flat anytime", " ".join(home.tariff_windows))

    def test_manual_tariff_fallback_saves_manual_rates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"

            updated = web.update_billing_cycle_settings(
                settings,
                billing_period_start_day="15",
                state_or_territory="WA",
                retailer="manual_tariff",
                current_plan="manual_tariff",
                manual_rates={
                    "supply_charge_per_day": "1.10",
                    "export_credit_rate": "0.03",
                    "super_off_peak": "0.11",
                    "overnight": "0.22",
                    "off_peak": "0.33",
                    "peak": "0.44",
                },
            )

        self.assertEqual(updated["tariff_setup_mode"], "manual")
        self.assertEqual(updated["retailer"], "Manual tariff")
        self.assertEqual(updated["current_plan"], "manual_tariff")
        self.assertAlmostEqual(updated["supply_charge_per_day"], 1.10)
        self.assertAlmostEqual(updated["export_credit_rate"], 0.03)
        self.assertEqual(updated["import_rates"]["peak"], 0.44)

    def test_billing_cycle_start_day_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            with self.assertRaises(ValueError):
                web.update_billing_cycle_settings(
                    settings,
                    billing_period_start_day="31",
                    state_or_territory="WA",
                    retailer="",
                    current_plan="synergy_ev_add_on",
                )

    def test_provider_payload_normalizes_mapping_fields(self):
        payload = web.normalize_provider_payload(
            {
                "endpoint": "http://homeassistant.local:8123",
                "entity_mappings.solar_live_power": "sensor.solar_power",
                "entity_mappings.battery_soc": "sensor.battery_soc",
            }
        )

        self.assertEqual(payload["endpoint"], "http://homeassistant.local:8123")
        self.assertEqual(payload["entity_mappings"]["solar_live_power"], "sensor.solar_power")
        self.assertEqual(payload["entity_mappings"]["battery_soc"], "sensor.battery_soc")


if __name__ == "__main__":
    unittest.main()
