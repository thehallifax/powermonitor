import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from engine.goodwe_provider import GoodWeApiError, GoodWeMeasurementProvider
from engine.measurement_framework import MeasurementQuality, MeasurementType


GOODWE_IMPORT_RUNTIME = {
    "timestamp": "2026-07-07T10:15:00+08:00",
    "e_day": 14.25,
    "ppv": 4120,
    "house_consumption": 2850,
    "pgrid": 650,
    "battery_power": -900,
}


GOODWE_EXPORT_RUNTIME = {
    "timestamp": "2026-07-07T13:30:00+08:00",
    "pv_power": 3600,
    "load_power": 1800,
    "p_grid": -720,
    "battery_power": 450,
}


GOODWE_DIRECT_GRID_RUNTIME = {
    "timestamp": "2026-07-07T16:45:00+08:00",
    "grid_import_power": 210,
    "grid_export_power": 0,
    "solar_power": 1100,
}


class GoodWeMeasurementProviderTests(unittest.TestCase):
    def test_requires_local_host_configuration(self):
        with self.assertRaises(ValueError):
            GoodWeMeasurementProvider("")

    def test_builds_from_host_config(self):
        provider = GoodWeMeasurementProvider.from_config({"host": "goodwe.local", "port": 8899})

        self.assertEqual(provider.provider_id, "goodwe")
        self.assertEqual(provider.host, "goodwe.local")
        self.assertEqual(provider.port, 8899)

    def test_capabilities_are_based_on_runtime_fields(self):
        provider = GoodWeMeasurementProvider("192.168.1.60", runtime_reader=lambda: GOODWE_IMPORT_RUNTIME)
        capabilities = provider.capabilities()
        by_type_unit = {(capability.measurement_type, capability.unit): capability for capability in capabilities}

        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "kWh")].quality, MeasurementQuality.MEASURED)
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].quality, MeasurementQuality.MEASURED)
        self.assertEqual(by_type_unit[(MeasurementType.HOUSEHOLD_CONSUMPTION, "W")].quality, MeasurementQuality.MEASURED)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].quality, MeasurementQuality.CALCULATED)
        self.assertEqual(by_type_unit[(MeasurementType.BATTERY_CHARGE, "W")].quality, MeasurementQuality.CALCULATED)
        self.assertNotIn((MeasurementType.GRID_EXPORT, "W"), by_type_unit)

    def test_maps_importing_runtime_data_to_measurements(self):
        provider = GoodWeMeasurementProvider("goodwe.local", runtime_reader=lambda: GOODWE_IMPORT_RUNTIME)
        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        by_type_unit = {(measurement.measurement_type, measurement.unit): measurement for measurement in measurements}

        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "kWh")].value, 14.25)
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].value, 4120.0)
        self.assertEqual(by_type_unit[(MeasurementType.HOUSEHOLD_CONSUMPTION, "W")].value, 2850.0)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].value, 650.0)
        self.assertEqual(by_type_unit[(MeasurementType.BATTERY_CHARGE, "W")].value, 900.0)
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].metadata.provider_id, "goodwe")
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].metadata.source, "goodwe_local")
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].quality, MeasurementQuality.CALCULATED)
        self.assertEqual(by_type_unit[(MeasurementType.BATTERY_CHARGE, "W")].quality, MeasurementQuality.CALCULATED)

    def test_maps_exporting_and_battery_discharge_runtime_data(self):
        provider = GoodWeMeasurementProvider("goodwe.local", runtime_reader=lambda: GOODWE_EXPORT_RUNTIME)
        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        by_type_unit = {(measurement.measurement_type, measurement.unit): measurement for measurement in measurements}

        self.assertEqual(by_type_unit[(MeasurementType.GRID_EXPORT, "W")].value, 720.0)
        self.assertEqual(by_type_unit[(MeasurementType.BATTERY_DISCHARGE, "W")].value, 450.0)
        self.assertNotIn((MeasurementType.GRID_IMPORT, "W"), by_type_unit)

    def test_direct_grid_import_values_are_measured(self):
        provider = GoodWeMeasurementProvider("goodwe.local", runtime_reader=lambda: GOODWE_DIRECT_GRID_RUNTIME)
        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        by_type_unit = {(measurement.measurement_type, measurement.unit): measurement for measurement in measurements}

        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].value, 210.0)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].quality, MeasurementQuality.MEASURED)

    def test_partial_payload_only_advertises_available_measurements(self):
        provider = GoodWeMeasurementProvider(
            "goodwe.local",
            runtime_reader=lambda: {"timestamp": "2026-07-07T11:00:00+08:00", "ppv": 1200},
        )

        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        capabilities = provider.capabilities()

        self.assertEqual(len(measurements), 1)
        self.assertEqual(measurements[0].measurement_type, MeasurementType.SOLAR_GENERATION)
        self.assertEqual([(capability.measurement_type, capability.unit) for capability in capabilities], [(MeasurementType.SOLAR_GENERATION, "W")])

    def test_filters_current_measurement_outside_requested_period(self):
        provider = GoodWeMeasurementProvider("goodwe.local", runtime_reader=lambda: GOODWE_IMPORT_RUNTIME)

        self.assertEqual(provider.measurements(date(2026, 7, 6), date(2026, 7, 6)), [])

    def test_uses_clock_timestamp_when_runtime_data_has_no_timestamp(self):
        provider = GoodWeMeasurementProvider(
            "goodwe.local",
            runtime_reader=lambda: {"ppv": 1000},
            now=lambda: datetime(2026, 7, 7, 9, 0, tzinfo=timezone.utc),
        )

        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))

        self.assertEqual(measurements[0].timestamp, "2026-07-07T09:00:00+00:00")

    def test_raises_when_optional_goodwe_package_is_missing(self):
        with patch("engine.goodwe_provider.importlib.import_module", side_effect=ImportError("missing")):
            provider = GoodWeMeasurementProvider("goodwe.local")

            with self.assertRaises(GoodWeApiError):
                provider.measurements(date(2026, 7, 7), date(2026, 7, 7))


if __name__ == "__main__":
    unittest.main()
