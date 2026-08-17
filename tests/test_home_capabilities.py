import sqlite3
from contextlib import closing
import unittest
from datetime import date

from engine.data_engine import DataEngine
from engine.home_capabilities import detect_home_capabilities
from engine.home_profile import HomeProfile
from engine.measurement_framework import (
    MeasurementCapability,
    MeasurementProviderRegistry,
    MeasurementQuality,
    MeasurementType,
)


class HomeCapabilityDetectionTests(unittest.TestCase):
    def test_bill_only_home_supports_billing_but_not_energy_analysis(self):
        capabilities = detect_home_capabilities(
            HomeProfile(electricity_retailer="Synergy", current_tariff="Home Plan"),
            MeasurementProviderRegistry([]),
        )

        self.assertCapability(capabilities, "billing_analysis", enabled=True, available=True, confidence="Medium")
        self.assertCapability(capabilities, "solar_generation_analysis", enabled=False, available=False)
        self.assertCapability(capabilities, "energy_timeline_readiness", enabled=True, available=False)
        self.assertCapability(capabilities, "home_intelligence_readiness", enabled=True, available=False)

    def test_solar_only_home_needs_solar_measurements_for_solar_analysis(self):
        profile = HomeProfile(solar_installed=True, current_tariff="Home Plan")
        capabilities = detect_home_capabilities(
            profile,
            registry_with(provider("solar_api", [cap(MeasurementType.SOLAR_GENERATION)])),
        )

        solar = capabilities.get("solar_generation_analysis")
        self.assertTrue(solar.enabled)
        self.assertTrue(solar.available)
        self.assertEqual(solar.confidence, "High")
        self.assertIn("provider:solar_api:solar_generation", solar.supporting_data_sources)
        self.assertCapability(capabilities, "household_consumption_analysis", enabled=False, available=False)

    def test_solar_plus_consumption_monitoring_enables_consumption_and_intelligence(self):
        profile = HomeProfile(
            current_tariff="EV Add On",
            solar_installed=True,
            household_monitoring_available=True,
        )
        capabilities = detect_home_capabilities(
            profile,
            registry_with(
                provider(
                    "hybrid_inverter",
                    [
                        cap(MeasurementType.SOLAR_GENERATION),
                        cap(MeasurementType.HOUSEHOLD_CONSUMPTION, MeasurementQuality.CALCULATED),
                        cap(MeasurementType.GRID_IMPORT),
                        cap(MeasurementType.GRID_EXPORT),
                    ],
                )
            ),
        )

        self.assertCapability(capabilities, "solar_generation_analysis", enabled=True, available=True)
        self.assertCapability(capabilities, "household_consumption_analysis", enabled=True, available=True, confidence="Medium")
        self.assertCapability(capabilities, "grid_import_export_analysis", enabled=True, available=True)
        self.assertCapability(capabilities, "home_intelligence_readiness", enabled=True, available=True)

    def test_ev_only_home_with_manual_charging_intent_has_declared_low_confidence_support(self):
        capabilities = detect_home_capabilities(
            HomeProfile(ev_owned=True, charger_type="Manual schedule", current_tariff="Home Plan"),
            MeasurementProviderRegistry([]),
        )

        ev = capabilities.get("ev_charging_analysis")
        self.assertTrue(ev.enabled)
        self.assertTrue(ev.available)
        self.assertEqual(ev.confidence, "Low")
        self.assertEqual(ev.data_quality, "declared")
        self.assertIn("home_profile:charger_type", ev.supporting_data_sources)

    def test_battery_declared_but_no_battery_measurements_is_not_available(self):
        capabilities = detect_home_capabilities(
            HomeProfile(battery_installed=True, battery_capacity_kwh=10.0),
            registry_with(provider("solar_api", [cap(MeasurementType.SOLAR_GENERATION)])),
        )

        battery = capabilities.get("battery_analysis")
        self.assertTrue(battery.enabled)
        self.assertFalse(battery.available)
        self.assertIn("Battery is declared", battery.reason)
        self.assertIn("home_profile:battery_installed", battery.supporting_data_sources)

    def test_overlapping_measurement_providers_are_preserved_as_supporting_sources(self):
        capabilities = detect_home_capabilities(
            HomeProfile(current_tariff="EV Add On"),
            registry_with(
                provider("solaredge", [cap(MeasurementType.GRID_IMPORT)]),
                provider("shelly", [cap(MeasurementType.GRID_IMPORT, MeasurementQuality.ESTIMATED)]),
                provider("tariff_file", [cap(MeasurementType.TARIFF_DATA)]),
            ),
        )

        tariff = capabilities.get("tariff_optimisation_readiness")
        self.assertTrue(tariff.available)
        self.assertEqual(tariff.confidence, "High")
        self.assertEqual(
            tariff.supporting_data_sources,
            [
                "home_profile:current_tariff",
                "provider:shelly:grid_import",
                "provider:solaredge:grid_import",
                "provider:tariff_file:tariff_data",
            ],
        )

    def test_missing_profile_defaults_disable_optional_home_capabilities(self):
        capabilities = detect_home_capabilities(None, MeasurementProviderRegistry([]))

        self.assertCapability(capabilities, "billing_analysis", enabled=True, available=False)
        self.assertCapability(capabilities, "solar_generation_analysis", enabled=False, available=False)
        self.assertCapability(capabilities, "ev_charging_analysis", enabled=False, available=False)
        self.assertCapability(capabilities, "battery_analysis", enabled=False, available=False)

    def test_data_engine_can_build_home_capabilities_from_measurement_registry(self):
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            create_readings(conn)
            engine = DataEngine(connection=conn)

            capabilities = engine.get_home_capabilities(
                HomeProfile(current_tariff="EV Add On", solar_installed=True),
                date(2026, 7, 7),
                date(2026, 7, 7),
            )

        self.assertCapability(capabilities, "solar_generation_analysis", enabled=True, available=True)
        self.assertCapability(capabilities, "grid_import_export_analysis", enabled=True, available=True)
        self.assertCapability(capabilities, "tariff_optimisation_readiness", enabled=True, available=True)

    def assertCapability(self, capabilities, capability_id, *, enabled, available, confidence=None):
        capability = capabilities.get(capability_id)
        self.assertIsNotNone(capability, capability_id)
        self.assertEqual(capability.enabled, enabled, capability_id)
        self.assertEqual(capability.available, available, capability_id)
        if confidence is not None:
            self.assertEqual(capability.confidence, confidence, capability_id)


class FakeMeasurementProvider:
    provider_name = "Fixture"

    def __init__(self, provider_id, capabilities):
        self.provider_id = provider_id
        self._capabilities = list(capabilities)

    def capabilities(self):
        return list(self._capabilities)

    def measurements(self, start, end):
        return []


def provider(provider_id, capabilities):
    return FakeMeasurementProvider(provider_id, capabilities)


def cap(measurement_type, quality=MeasurementQuality.MEASURED):
    return MeasurementCapability(measurement_type, "kWh", quality)


def registry_with(*providers):
    return MeasurementProviderRegistry(list(providers))


def create_readings(conn):
    conn.execute(
        """
        CREATE TABLE readings (
            household_id TEXT DEFAULT 'default_household',
            measurement_time TEXT PRIMARY KEY,
            tariff_bucket TEXT,
            production_kwh REAL,
            solar_to_home_kwh REAL,
            solar_to_grid_kwh REAL,
            grid_to_home_w REAL,
            grid_to_home_kwh REAL,
            import_cost REAL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO readings (
            household_id, measurement_time, tariff_bucket, production_kwh,
            solar_to_home_kwh, solar_to_grid_kwh, grid_to_home_w, grid_to_home_kwh,
            import_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "default_household",
            "2026-07-07T10:00:00+08:00",
            "super_off_peak",
            1.0,
            0.7,
            0.3,
            1000.0,
            0.25,
            0.02,
        ),
    )


if __name__ == "__main__":
    unittest.main()
