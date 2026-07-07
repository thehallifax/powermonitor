import unittest
from datetime import date, datetime, timezone

from engine.fronius_provider import FroniusApiError, FroniusMeasurementProvider
from engine.measurement_framework import MeasurementQuality, MeasurementType


POWER_FLOW_IMPORT = {
    "Body": {
        "Data": {
            "Site": {
                "E_Day": 12345,
                "P_Grid": 620.0,
                "P_Load": -2180.0,
                "P_PV": 3560.0,
            }
        }
    },
    "Head": {
        "Status": {"Code": 0, "Reason": "", "UserMessage": ""},
        "Timestamp": "2026-07-07T10:15:00+08:00",
    },
}


POWER_FLOW_EXPORT = {
    "Body": {
        "Data": {
            "Site": {
                "P_Grid": -725.0,
                "P_Load": -1900.0,
                "P_PV": 2625.0,
            }
        }
    },
    "Head": {
        "Status": {"Code": 0, "Reason": "", "UserMessage": ""},
        "Timestamp": "2026-07-07T13:30:00+08:00",
    },
}


class FroniusMeasurementProviderTests(unittest.TestCase):
    def test_requires_local_host_configuration(self):
        with self.assertRaises(ValueError):
            FroniusMeasurementProvider("")

    def test_builds_from_host_config(self):
        provider = FroniusMeasurementProvider.from_config({"host": "fronius.local"})

        self.assertEqual(provider.provider_id, "fronius")
        self.assertEqual(provider.base_url, "http://fronius.local/")

    def test_reports_fronius_capabilities(self):
        provider = FroniusMeasurementProvider("192.168.1.50", http_get=lambda url: POWER_FLOW_IMPORT)
        capabilities = provider.capabilities()

        by_type_unit = {(capability.measurement_type, capability.unit): capability for capability in capabilities}
        self.assertEqual(
            by_type_unit[(MeasurementType.SOLAR_GENERATION, "kWh")].quality,
            MeasurementQuality.MEASURED,
        )
        self.assertEqual(
            by_type_unit[(MeasurementType.HOUSEHOLD_CONSUMPTION, "W")].quality,
            MeasurementQuality.MEASURED,
        )
        self.assertEqual(
            by_type_unit[(MeasurementType.GRID_IMPORT, "W")].quality,
            MeasurementQuality.CALCULATED,
        )
        self.assertEqual(
            by_type_unit[(MeasurementType.GRID_EXPORT, "W")].quality,
            MeasurementQuality.CALCULATED,
        )

    def test_maps_power_flow_import_payload_to_measurements(self):
        requested_urls = []

        def http_get(url):
            requested_urls.append(url)
            return POWER_FLOW_IMPORT

        provider = FroniusMeasurementProvider("fronius.local", http_get=http_get)
        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        by_type_unit = {(measurement.measurement_type, measurement.unit): measurement for measurement in measurements}

        self.assertEqual(requested_urls, ["http://fronius.local/solar_api/v1/GetPowerFlowRealtimeData.fcgi"])
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "kWh")].value, 12.345)
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].value, 3560.0)
        self.assertEqual(by_type_unit[(MeasurementType.HOUSEHOLD_CONSUMPTION, "W")].value, 2180.0)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].value, 620.0)
        self.assertNotIn((MeasurementType.GRID_EXPORT, "W"), by_type_unit)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_IMPORT, "W")].quality, MeasurementQuality.CALCULATED)
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].metadata.provider_id, "fronius")
        self.assertEqual(by_type_unit[(MeasurementType.SOLAR_GENERATION, "W")].metadata.source, "fronius_solar_api")

    def test_maps_negative_grid_power_to_grid_export(self):
        provider = FroniusMeasurementProvider("http://fronius.local", http_get=lambda url: POWER_FLOW_EXPORT)
        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        by_type_unit = {(measurement.measurement_type, measurement.unit): measurement for measurement in measurements}

        self.assertEqual(by_type_unit[(MeasurementType.GRID_EXPORT, "W")].value, 725.0)
        self.assertEqual(by_type_unit[(MeasurementType.GRID_EXPORT, "W")].quality, MeasurementQuality.CALCULATED)
        self.assertNotIn((MeasurementType.GRID_IMPORT, "W"), by_type_unit)

    def test_filters_current_measurement_outside_requested_period(self):
        provider = FroniusMeasurementProvider("fronius.local", http_get=lambda url: POWER_FLOW_IMPORT)

        self.assertEqual(provider.measurements(date(2026, 7, 6), date(2026, 7, 6)), [])

    def test_uses_clock_timestamp_when_payload_has_no_timestamp(self):
        payload = {"Body": {"Data": {"Site": {"P_PV": 1000}}}, "Head": {"Status": {"Code": 0}}}
        provider = FroniusMeasurementProvider(
            "fronius.local",
            http_get=lambda url: payload,
            now=lambda: datetime(2026, 7, 7, 9, 0, tzinfo=timezone.utc),
        )

        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))

        self.assertEqual(measurements[0].timestamp, "2026-07-07T09:00:00+00:00")

    def test_raises_when_fronius_status_is_not_successful(self):
        payload = {
            "Body": {"Data": {}},
            "Head": {"Status": {"Code": 255, "Reason": "Device unavailable"}},
        }
        provider = FroniusMeasurementProvider("fronius.local", http_get=lambda url: payload)

        with self.assertRaises(FroniusApiError):
            provider.measurements(date(2026, 7, 7), date(2026, 7, 7))


if __name__ == "__main__":
    unittest.main()
