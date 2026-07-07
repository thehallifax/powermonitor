import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from engine.measurement_framework import MeasurementQuality, MeasurementType
from engine.sungrow_provider import SungrowApiError, SungrowMeasurementProvider


SUNGROW_IMPORT_RUNTIME = {
    "timestamp": "2026-07-07T10:15:00+08:00",
    "daily_pv_generation": 18.75,
    "total_dc_power": 5200,
    "load_power": 3400,
    "meter_active_power": 700,
    "battery_power": -1200,
}


SUNGROW_EXPORT_RUNTIME = {
    "timestamp": "2026-07-07T13:30:00+08:00",
    "pv_power": 4100,
    "household_power": 2100,
    "p_grid": -950,
    "battery_power": 600,
}


SUNGROW_DIRECT_RUNTIME = {
    "timestamp": "2026-07-07T16:45:00+08:00",
    "grid_import_power": 220,
    "grid_export_power": 0,
    "battery_charge_power": 500,
    "solar_power": 1250,
}


class SungrowMeasurementProviderTests(unittest.TestCase):
    def test_requires_local_host_configuration(self):
        with self.assertRaises(ValueError):
            SungrowMeasurementProvider("")

    def test_builds_from_host_config(self):
        provider = SungrowMeasurementProvider.from_config(
            {
                "host": "sungrow.local",
                "port": 516,
                "protocol": "modbus_tcp",
                "unit_id": 2,
                "register_map": {"pv_power": 5000},
            }
        )

        self.assertEqual(provider.provider_id, "sungrow")
        self.assertEqual(provider.host, "sungrow.local")
        self.assertEqual(provider.port, 516)
        self.assertEqual(provider.protocol, "modbus_tcp")
        self.assertEqual(provider.unit_id, 2)
        self.assertEqual(provider.register_map, {"pv_power": 5000})

    def test_capabilities_are_based_on_runtime_fields(self):
        provider = SungrowMeasurementProvider("192.168.1.70", runtime_reader=lambda: SUNGROW_IMPORT_RUNTIME)
        capabilities = provider.capabilities()
        by_type_unit = {(capability.measurement_type, capability.unit): capability for capability in capabilities}

        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "kWh")].quality, MeasurementQuality.MEASURED)
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].quality, MeasurementQuality.MEASURED)
        self.assertEqual(by_type_unit[(MeasurementType.HOUSEHOLD_CONSUMPTION, "W")].quality, MeasurementQuality.MEASURED)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].quality, MeasurementQuality.CALCULATED)
        self.assertEqual(by_type_unit[(MeasurementType.BATTERY_CHARGE, "W")].quality, MeasurementQuality.CALCULATED)
        self.assertNotIn((MeasurementType.GRID_EXPORT, "W"), by_type_unit)

    def test_maps_importing_runtime_data_to_measurements(self):
        provider = SungrowMeasurementProvider("sungrow.local", runtime_reader=lambda: SUNGROW_IMPORT_RUNTIME)
        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        by_type_unit = {(measurement.measurement_type, measurement.unit): measurement for measurement in measurements}

        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "kWh")].value, 18.75)
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].value, 5200.0)
        self.assertEqual(by_type_unit[(MeasurementType.HOUSEHOLD_CONSUMPTION, "W")].value, 3400.0)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].value, 700.0)
        self.assertEqual(by_type_unit[(MeasurementType.BATTERY_CHARGE, "W")].value, 1200.0)
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].metadata.provider_id, "sungrow")
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].metadata.source, "sungrow_local")
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].quality, MeasurementQuality.CALCULATED)

    def test_maps_exporting_and_battery_discharge_runtime_data(self):
        provider = SungrowMeasurementProvider("sungrow.local", runtime_reader=lambda: SUNGROW_EXPORT_RUNTIME)
        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        by_type_unit = {(measurement.measurement_type, measurement.unit): measurement for measurement in measurements}

        self.assertEqual(by_type_unit[(MeasurementType.GRID_EXPORT, "W")].value, 950.0)
        self.assertEqual(by_type_unit[(MeasurementType.BATTERY_DISCHARGE, "W")].value, 600.0)
        self.assertNotIn((MeasurementType.GRID_IMPORT, "W"), by_type_unit)

    def test_direct_grid_and_battery_values_are_measured(self):
        provider = SungrowMeasurementProvider("sungrow.local", runtime_reader=lambda: SUNGROW_DIRECT_RUNTIME)
        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        by_type_unit = {(measurement.measurement_type, measurement.unit): measurement for measurement in measurements}

        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].value, 220.0)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].quality, MeasurementQuality.MEASURED)
        self.assertEqual(by_type_unit[(MeasurementType.BATTERY_CHARGE, "W")].value, 500.0)
        self.assertEqual(by_type_unit[(MeasurementType.BATTERY_CHARGE, "W")].quality, MeasurementQuality.MEASURED)

    def test_partial_payload_only_advertises_available_measurements(self):
        provider = SungrowMeasurementProvider(
            "sungrow.local",
            runtime_reader=lambda: {"timestamp": "2026-07-07T11:00:00+08:00", "pv_power": 1300},
        )

        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        capabilities = provider.capabilities()

        self.assertEqual(len(measurements), 1)
        self.assertEqual(measurements[0].measurement_type, MeasurementType.SOLAR_GENERATION)
        self.assertEqual([(capability.measurement_type, capability.unit) for capability in capabilities], [(MeasurementType.SOLAR_GENERATION, "W")])

    def test_filters_current_measurement_outside_requested_period(self):
        provider = SungrowMeasurementProvider("sungrow.local", runtime_reader=lambda: SUNGROW_IMPORT_RUNTIME)

        self.assertEqual(provider.measurements(date(2026, 7, 6), date(2026, 7, 6)), [])

    def test_uses_clock_timestamp_when_runtime_data_has_no_timestamp(self):
        provider = SungrowMeasurementProvider(
            "sungrow.local",
            runtime_reader=lambda: {"pv_power": 1000},
            now=lambda: datetime(2026, 7, 7, 9, 0, tzinfo=timezone.utc),
        )

        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))

        self.assertEqual(measurements[0].timestamp, "2026-07-07T09:00:00+00:00")

    def test_raises_without_register_map_when_no_runtime_reader_is_injected(self):
        provider = SungrowMeasurementProvider("sungrow.local")

        with self.assertRaises(SungrowApiError):
            provider.measurements(date(2026, 7, 7), date(2026, 7, 7))

    def test_raises_when_optional_pymodbus_package_is_missing(self):
        with patch.dict("sys.modules", {"pymodbus.client": None}):
            provider = SungrowMeasurementProvider(
                "sungrow.local",
                register_map={"pv_power": {"address": 5000, "scale": 10}},
            )

            with self.assertRaises(SungrowApiError):
                provider.measurements(date(2026, 7, 7), date(2026, 7, 7))


if __name__ == "__main__":
    unittest.main()
