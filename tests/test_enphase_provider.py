import unittest
from datetime import date, datetime, timezone

from engine.enphase_provider import EnphaseMeasurementProvider
from engine.measurement_framework import MeasurementQuality, MeasurementType


PRODUCTION_IMPORT_PAYLOAD = {
    "timestamp": "2026-07-07T10:15:00+08:00",
    "production": [
        {"type": "inverters", "wNow": 3200, "whToday": 10400},
        {"type": "eim", "wNow": 3350, "whToday": 10850},
    ],
    "consumption": [
        {"type": "eim", "measurementType": "total-consumption", "wNow": 4100, "whToday": 13000},
        {"type": "eim", "measurementType": "net-consumption", "wNow": 750},
    ],
    "storage": [{"type": "acb", "wNow": -500}],
}


PRODUCTION_EXPORT_PAYLOAD = {
    "timestamp": "2026-07-07T13:30:00+08:00",
    "production": [{"type": "eim", "wNow": 4600, "whToday": 16500}],
    "consumption": [
        {"measurementType": "total-consumption", "wNow": 1800},
        {"measurementType": "net-consumption", "wNow": -2800},
    ],
    "storage": [{"type": "acb", "wNow": 650}],
}


METER_READINGS_PAYLOAD = [
    {
        "measurementType": "production",
        "timestamp": 1783390500,
        "activePower": 2500,
    },
    {
        "measurementType": "total-consumption",
        "timestamp": 1783390500,
        "activePower": 1900,
    },
    {
        "measurementType": "net-consumption",
        "timestamp": 1783390500,
        "activePower": 600,
    },
]


class EnphaseMeasurementProviderTests(unittest.TestCase):
    def test_requires_local_host_configuration(self):
        with self.assertRaises(ValueError):
            EnphaseMeasurementProvider("")

    def test_builds_from_host_config(self):
        provider = EnphaseMeasurementProvider.from_config(
            {
                "host": "envoy.local",
                "token": "local-token",
                "username": "envoy",
                "password": "secret",
                "use_https": False,
            }
        )

        self.assertEqual(provider.provider_id, "enphase")
        self.assertEqual(provider.base_url, "http://envoy.local/")
        self.assertEqual(provider.token, "local-token")
        self.assertEqual(provider.username, "envoy")
        self.assertEqual(provider.password, "secret")

    def test_capabilities_are_based_on_runtime_fields(self):
        provider = EnphaseMeasurementProvider(
            "envoy.local",
            runtime_reader=lambda: {"production_json": PRODUCTION_IMPORT_PAYLOAD},
        )
        capabilities = provider.capabilities()
        by_type_unit = {(capability.measurement_type, capability.unit): capability for capability in capabilities}

        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "kWh")].quality, MeasurementQuality.MEASURED)
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].quality, MeasurementQuality.MEASURED)
        self.assertEqual(by_type_unit[(MeasurementType.HOUSEHOLD_CONSUMPTION, "W")].quality, MeasurementQuality.MEASURED)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].quality, MeasurementQuality.CALCULATED)
        self.assertEqual(by_type_unit[(MeasurementType.BATTERY_CHARGE, "W")].quality, MeasurementQuality.CALCULATED)

    def test_maps_production_json_import_payload_to_measurements(self):
        provider = EnphaseMeasurementProvider(
            "envoy.local",
            runtime_reader=lambda: {"production_json": PRODUCTION_IMPORT_PAYLOAD},
        )
        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        by_type_unit = {(measurement.measurement_type, measurement.unit): measurement for measurement in measurements}

        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "kWh")].value, 10.85)
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].value, 3350.0)
        self.assertEqual(by_type_unit[(MeasurementType.HOUSEHOLD_CONSUMPTION, "W")].value, 4100.0)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].value, 750.0)
        self.assertEqual(by_type_unit[(MeasurementType.BATTERY_CHARGE, "W")].value, 500.0)
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].metadata.provider_id, "enphase")
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].metadata.source, "enphase_envoy_local")
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].quality, MeasurementQuality.CALCULATED)

    def test_maps_production_json_export_and_battery_discharge(self):
        provider = EnphaseMeasurementProvider(
            "envoy.local",
            runtime_reader=lambda: {"production_json": PRODUCTION_EXPORT_PAYLOAD},
        )
        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        by_type_unit = {(measurement.measurement_type, measurement.unit): measurement for measurement in measurements}

        self.assertEqual(by_type_unit[(MeasurementType.GRID_EXPORT, "W")].value, 2800.0)
        self.assertEqual(by_type_unit[(MeasurementType.BATTERY_DISCHARGE, "W")].value, 650.0)
        self.assertNotIn((MeasurementType.GRID_IMPORT, "W"), by_type_unit)

    def test_maps_meter_readings_payload_when_production_json_is_unavailable(self):
        provider = EnphaseMeasurementProvider(
            "envoy.local",
            runtime_reader=lambda: {"meter_readings": METER_READINGS_PAYLOAD},
        )
        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        by_type_unit = {(measurement.measurement_type, measurement.unit): measurement for measurement in measurements}

        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].value, 2500.0)
        self.assertEqual(by_type_unit[(MeasurementType.HOUSEHOLD_CONSUMPTION, "W")].value, 1900.0)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_EXPORT, "W")].value, 600.0)
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].quality, MeasurementQuality.MEASURED)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_EXPORT, "W")].quality, MeasurementQuality.CALCULATED)

    def test_partial_payload_only_advertises_available_measurements(self):
        provider = EnphaseMeasurementProvider(
            "envoy.local",
            runtime_reader=lambda: {
                "production_json": {
                    "timestamp": "2026-07-07T11:00:00+08:00",
                    "production": [{"type": "inverters", "wNow": 1200}],
                }
            },
        )

        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        capabilities = provider.capabilities()

        self.assertEqual(len(measurements), 1)
        self.assertEqual(measurements[0].measurement_type, MeasurementType.SOLAR_GENERATION)
        self.assertEqual([(capability.measurement_type, capability.unit) for capability in capabilities], [(MeasurementType.SOLAR_GENERATION, "W")])

    def test_filters_current_measurement_outside_requested_period(self):
        provider = EnphaseMeasurementProvider(
            "envoy.local",
            runtime_reader=lambda: {"production_json": PRODUCTION_IMPORT_PAYLOAD},
        )

        self.assertEqual(provider.measurements(date(2026, 7, 6), date(2026, 7, 6)), [])

    def test_uses_clock_timestamp_when_payload_has_no_timestamp(self):
        provider = EnphaseMeasurementProvider(
            "envoy.local",
            runtime_reader=lambda: {"production_json": {"production": [{"wNow": 1000}]}},
            now=lambda: datetime(2026, 7, 7, 9, 0, tzinfo=timezone.utc),
        )

        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))

        self.assertEqual(measurements[0].timestamp, "2026-07-07T09:00:00+00:00")

    def test_local_http_fetch_uses_bearer_token_and_envoy_endpoints(self):
        requested = []

        def http_get(url, headers):
            requested.append((url, headers))
            if url.endswith("/production.json"):
                return PRODUCTION_IMPORT_PAYLOAD
            return METER_READINGS_PAYLOAD

        provider = EnphaseMeasurementProvider(
            "envoy.local",
            token="local-token",
            use_https=True,
            http_get=http_get,
        )
        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))

        self.assertTrue(measurements)
        self.assertEqual(
            [item[0] for item in requested],
            [
                "https://envoy.local/production.json",
                "https://envoy.local/ivp/meters/readings",
            ],
        )
        self.assertEqual(requested[0][1]["Authorization"], "Bearer local-token")


if __name__ == "__main__":
    unittest.main()
