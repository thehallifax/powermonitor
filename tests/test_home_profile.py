import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import report
from engine.home_profile import HomeProfile, load_home_profile, save_home_profile


class HomeProfileTests(unittest.TestCase):
    def test_missing_profile_uses_settings_backed_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "home_profile.json"

            profile = load_home_profile(
                path,
                settings={
                    "state_or_territory": "WA",
                    "retailer": "Synergy",
                    "current_plan": "synergy_ev_add_on",
                },
            )

        self.assertEqual(profile.location_region, "WA")
        self.assertEqual(profile.electricity_retailer, "Synergy")
        self.assertEqual(profile.current_tariff, "synergy_ev_add_on")
        self.assertFalse(profile.solar_installed)
        self.assertFalse(profile.ev_owned)

    def test_profile_round_trip_preserves_declared_home_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "home_profile.json"
            profile = HomeProfile(
                location_region="WA",
                electricity_retailer="Synergy",
                current_tariff="EV Add On",
                solar_installed=True,
                solar_capacity_kw=6.6,
                inverter_brand="Fronius",
                inverter_model="Primo",
                battery_installed=True,
                battery_capacity_kwh=10.0,
                ev_owned=True,
                vehicle_make="Tesla",
                vehicle_model="Model 3",
                charger_type="Ocular",
                household_monitoring_available=True,
                home_assistant_available=True,
            )

            save_home_profile(profile, path)
            loaded = load_home_profile(path)

        self.assertEqual(loaded, profile)

    def test_unknown_fields_are_preserved_for_forward_compatibility(self):
        profile = HomeProfile.from_dict(
            {
                "location_region": "WA",
                "solar_installed": "yes",
                "future_heat_pump_installed": True,
                "extra_fields": {"roof_orientation": "north"},
            }
        )

        payload = profile.as_dict()

        self.assertTrue(profile.solar_installed)
        self.assertTrue(payload["future_heat_pump_installed"])
        self.assertEqual(payload["roof_orientation"], "north")

    def test_invalid_profile_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "home_profile.json"
            path.write_text("{not json", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_home_profile(path)

    def test_report_settings_attach_home_profile_without_changing_settings_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            profile_path = Path(temp_dir) / "home_profile.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "state_or_territory": "WA",
                        "retailer": "Synergy",
                        "current_plan": "synergy_home_plan",
                    }
                ),
                encoding="utf-8",
            )
            profile_path.write_text(
                json.dumps(
                    {
                        "location_region": "NSW",
                        "electricity_retailer": "Example Retailer",
                        "current_tariff": "Example Plan",
                        "ev_owned": True,
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(report, "SETTINGS_PATH", settings_path):
                with patch.object(report, "HOME_PROFILE_PATH", profile_path):
                    settings = report.load_settings()

            saved_settings = json.loads(settings_path.read_text(encoding="utf-8"))

        self.assertEqual(settings.current_plan, "synergy_home_plan")
        self.assertEqual(settings.home_profile.location_region, "NSW")
        self.assertEqual(settings.home_profile.electricity_retailer, "Example Retailer")
        self.assertTrue(settings.home_profile.ev_owned)
        self.assertNotIn("home_profile", saved_settings)


if __name__ == "__main__":
    unittest.main()
