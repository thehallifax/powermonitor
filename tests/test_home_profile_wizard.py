import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import setup_home_profile
from engine.home_profile import HomeProfile, load_home_profile, save_home_profile


class HomeProfileWizardTests(unittest.TestCase):
    def test_wizard_creates_profile_with_core_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "home_profile.json"

            profile = setup_home_profile.run_home_profile_wizard(
                path,
                input_func=answers(
                    "WA",
                    "Synergy",
                    "EV Add On",
                    "yes",
                    "6.6",
                    "Fronius",
                    "Primo",
                    "no",
                    "yes",
                    "Tesla",
                    "Model 3",
                    "Ocular",
                    "yes",
                    "no",
                ),
                output_func=lambda _message: None,
            )
            loaded = load_home_profile(path)

        self.assertEqual(profile, loaded)
        self.assertEqual(loaded.location_region, "WA")
        self.assertEqual(loaded.electricity_retailer, "Synergy")
        self.assertEqual(loaded.current_tariff, "EV Add On")
        self.assertTrue(loaded.solar_installed)
        self.assertEqual(loaded.solar_capacity_kw, 6.6)
        self.assertEqual(loaded.inverter_brand, "Fronius")
        self.assertFalse(loaded.battery_installed)
        self.assertTrue(loaded.ev_owned)
        self.assertEqual(loaded.vehicle_model, "Model 3")
        self.assertTrue(loaded.household_monitoring_available)
        self.assertFalse(loaded.home_assistant_available)

    def test_wizard_updates_existing_profile_and_keeps_blank_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "home_profile.json"
            save_home_profile(
                HomeProfile(
                    location_region="WA",
                    electricity_retailer="Old Retailer",
                    current_tariff="Old Plan",
                    solar_installed=True,
                    solar_capacity_kw=5.0,
                    inverter_brand="Old Brand",
                    inverter_model="Old Model",
                    battery_installed=False,
                    ev_owned=True,
                    vehicle_make="Old Make",
                    vehicle_model="Old Model",
                    charger_type="Old Charger",
                    household_monitoring_available=True,
                    home_assistant_available=False,
                ),
                path,
            )

            updated = setup_home_profile.run_home_profile_wizard(
                path,
                input_func=answers(
                    "",
                    "New Retailer",
                    "",
                    "no",
                    "yes",
                    "",
                    "no",
                    "",
                    "yes",
                ),
                output_func=lambda _message: None,
            )

        self.assertEqual(updated.location_region, "WA")
        self.assertEqual(updated.electricity_retailer, "New Retailer")
        self.assertEqual(updated.current_tariff, "Old Plan")
        self.assertFalse(updated.solar_installed)
        self.assertIsNone(updated.solar_capacity_kw)
        self.assertEqual(updated.inverter_brand, "")
        self.assertTrue(updated.battery_installed)
        self.assertIsNone(updated.battery_capacity_kwh)
        self.assertFalse(updated.ev_owned)
        self.assertEqual(updated.vehicle_make, "")
        self.assertTrue(updated.household_monitoring_available)
        self.assertTrue(updated.home_assistant_available)

    def test_wizard_allows_skipping_optional_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "home_profile.json"

            profile = setup_home_profile.run_home_profile_wizard(
                path,
                input_func=answers(
                    "WA",
                    "Synergy",
                    "Home Plan",
                    "yes",
                    "",
                    "",
                    "",
                    "yes",
                    "",
                    "yes",
                    "",
                    "",
                    "",
                    "no",
                    "no",
                ),
                output_func=lambda _message: None,
            )

        self.assertTrue(profile.solar_installed)
        self.assertIsNone(profile.solar_capacity_kw)
        self.assertEqual(profile.inverter_brand, "")
        self.assertTrue(profile.battery_installed)
        self.assertIsNone(profile.battery_capacity_kwh)
        self.assertTrue(profile.ev_owned)
        self.assertEqual(profile.vehicle_make, "")
        self.assertEqual(profile.charger_type, "")

    def test_wizard_reprompts_invalid_bool_and_number_responses(self):
        messages = []
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "home_profile.json"

            profile = setup_home_profile.run_home_profile_wizard(
                path,
                input_func=answers(
                    "WA",
                    "Synergy",
                    "EV Add On",
                    "maybe",
                    "yes",
                    "large",
                    "-1",
                    "6.6",
                    "",
                    "",
                    "no",
                    "no",
                    "",
                    "",
                ),
                output_func=messages.append,
            )

        self.assertTrue(profile.solar_installed)
        self.assertEqual(profile.solar_capacity_kw, 6.6)
        self.assertIn("Please answer yes or no.", messages)
        self.assertIn("Please enter a number, or leave it blank to skip.", messages)
        self.assertIn("Please enter zero or a positive number.", messages)


def answers(*values):
    pending = list(values)

    def respond(_prompt):
        if not pending:
            raise AssertionError("Wizard requested more answers than expected")
        return pending.pop(0)

    return respond


if __name__ == "__main__":
    unittest.main()
