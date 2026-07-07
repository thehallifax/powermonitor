import sqlite3
import unittest
from datetime import date

from engine.data_engine import DataEngine
from engine.measurement_framework import (
    MeasurementCapability,
    MeasurementMetadata,
    MeasurementProviderRegistry,
    MeasurementQuality,
    MeasurementType,
    SolarEdgeMeasurementProvider,
    UniversalMeasurement,
)


class UniversalMeasurementFrameworkTests(unittest.TestCase):
    def test_provider_registration_and_capability_detection(self):
        provider = SolarEdgeMeasurementProvider([])
        registry = MeasurementProviderRegistry([provider])

        self.assertEqual(registry.provider_ids(), ["solaredge"])
        self.assertTrue(registry.supports(MeasurementType.SOLAR_GENERATION))
        self.assertTrue(registry.supports(MeasurementType.GRID_IMPORT))
        self.assertFalse(registry.supports(MeasurementType.BATTERY_CHARGE))
        self.assertEqual(
            registry.supported_measurement_types(),
            [
                MeasurementType.GRID_EXPORT,
                MeasurementType.GRID_IMPORT,
                MeasurementType.HOUSEHOLD_CONSUMPTION,
                MeasurementType.SOLAR_GENERATION,
            ],
        )

    def test_duplicate_provider_ids_are_rejected(self):
        first = SolarEdgeMeasurementProvider([])
        second = SolarEdgeMeasurementProvider([])

        with self.assertRaises(ValueError):
            MeasurementProviderRegistry([first, second])

        registry = MeasurementProviderRegistry([first])
        with self.assertRaises(ValueError):
            registry.register(second)

    def test_solaredge_reading_maps_to_universal_measurements(self):
        provider = SolarEdgeMeasurementProvider(
            [
                {
                    "measurement_time": "2026-07-07T10:00:00+08:00",
                    "tariff_bucket": "super_off_peak",
                    "production_w": 4000,
                    "consumption_w": 2200,
                    "solar_to_home_w": 2000,
                    "solar_to_grid_w": 1800,
                    "grid_to_home_w": 200,
                    "production_kwh": 1.0,
                    "consumption_kwh": 0.55,
                    "solar_to_home_kwh": 0.5,
                    "solar_to_grid_kwh": 0.45,
                    "grid_to_home_kwh": 0.05,
                }
            ]
        )

        measurements = provider.measurements(date(2026, 7, 7), date(2026, 7, 7))
        by_type = {measurement.measurement_type: measurement for measurement in measurements}

        self.assertEqual(by_type[MeasurementType.SOLAR_GENERATION].value, 1.0)
        self.assertEqual(by_type[MeasurementType.GRID_EXPORT].value, 0.45)
        self.assertEqual(by_type[MeasurementType.GRID_IMPORT].attributes["tariff_bucket"], "super_off_peak")
        self.assertEqual(by_type[MeasurementType.SOLAR_GENERATION].attributes["solar_used_kwh"], 0.5)

    def test_data_quality_propagates_to_measurements(self):
        measurement = SolarEdgeMeasurementProvider(
            [
                {
                    "measurement_time": "2026-07-07T10:00:00+08:00",
                    "production_kwh": 1.0,
                    "solar_to_home_kwh": 0.5,
                    "solar_to_grid_kwh": 0.5,
                    "grid_to_home_kwh": 0.0,
                }
            ]
        ).measurements(date(2026, 7, 7), date(2026, 7, 7))[0]

        self.assertEqual(measurement.quality, MeasurementQuality.MEASURED)
        self.assertEqual(measurement.metadata.provider_id, "solaredge")
        self.assertEqual(measurement.metadata.source, "readings")

    def test_missing_partial_reading_data_is_normalized_to_zero_values(self):
        measurements = SolarEdgeMeasurementProvider(
            [
                {
                    "measurement_time": "2026-07-07T10:00:00+08:00",
                    "production_kwh": None,
                    "solar_to_home_kwh": None,
                    "solar_to_grid_kwh": "not-a-number",
                    "grid_to_home_kwh": None,
                }
            ]
        ).measurements(date(2026, 7, 7), date(2026, 7, 7))

        self.assertEqual(len(measurements), 4)
        self.assertEqual({measurement.value for measurement in measurements}, {0.0})
        self.assertEqual(
            {measurement.measurement_type for measurement in measurements},
            {
                MeasurementType.SOLAR_GENERATION,
                MeasurementType.HOUSEHOLD_CONSUMPTION,
                MeasurementType.GRID_IMPORT,
                MeasurementType.GRID_EXPORT,
            },
        )

    def test_data_engine_loads_measurement_registry_from_readings(self):
        with sqlite3.connect(":memory:") as conn:
            create_readings(conn)
            engine = DataEngine(connection=conn)

            registry = engine.get_measurement_provider_registry(date(2026, 7, 7), date(2026, 7, 7))
            measurements = engine.get_measurements(date(2026, 7, 7), date(2026, 7, 7), [MeasurementType.GRID_IMPORT])

        self.assertEqual(registry.provider_ids(), ["solaredge"])
        self.assertEqual(len(measurements), 1)
        self.assertEqual(measurements[0].value, 0.25)

    def test_missing_capability_returns_no_provider(self):
        registry = MeasurementProviderRegistry([SolarEdgeMeasurementProvider([])])

        self.assertEqual(registry.providers_for(MeasurementType.TARIFF_DATA), [])
        self.assertFalse(registry.supports(MeasurementType.TARIFF_DATA))
        self.assertEqual(
            registry.missing_capabilities([MeasurementType.GRID_IMPORT, MeasurementType.TARIFF_DATA]),
            [MeasurementType.TARIFF_DATA],
        )

    def test_multiple_providers_can_advertise_overlapping_capabilities(self):
        registry = MeasurementProviderRegistry(
            [
                SolarEdgeMeasurementProvider(
                    [
                        {
                            "measurement_time": "2026-07-07T10:00:00+08:00",
                            "production_kwh": 1.0,
                            "solar_to_home_kwh": 0.5,
                            "solar_to_grid_kwh": 0.5,
                            "grid_to_home_kwh": 0.25,
                        }
                    ]
                ),
                FakeMeasurementProvider(
                    "shelly",
                    [
                        MeasurementCapability(
                            MeasurementType.GRID_IMPORT,
                            "kWh",
                            MeasurementQuality.ESTIMATED,
                        )
                    ],
                    [
                        UniversalMeasurement(
                            MeasurementType.GRID_IMPORT,
                            0.2,
                            "kWh",
                            MeasurementMetadata(
                                provider_id="shelly",
                                source="fixture",
                                timestamp="2026-07-07T10:00:00+08:00",
                                quality=MeasurementQuality.ESTIMATED,
                            ),
                        )
                    ],
                ),
            ]
        )

        providers = registry.providers_for(MeasurementType.GRID_IMPORT)
        measurements = registry.measurements(date(2026, 7, 7), date(2026, 7, 7), [MeasurementType.GRID_IMPORT])

        self.assertEqual([provider.provider_id for provider in providers], ["solaredge", "shelly"])
        self.assertEqual([measurement.provider_id for measurement in measurements], ["shelly", "solaredge"])
        self.assertEqual(measurements[0].quality, MeasurementQuality.ESTIMATED)

    def test_unavailable_capability_is_reported_but_not_supported(self):
        registry = MeasurementProviderRegistry(
            [
                FakeMeasurementProvider(
                    "future_battery",
                    [
                        MeasurementCapability(
                            MeasurementType.BATTERY_CHARGE,
                            "kWh",
                            MeasurementQuality.UNKNOWN,
                            available=False,
                            note="Battery integration not configured.",
                        )
                    ],
                    [],
                )
            ]
        )

        self.assertFalse(registry.supports(MeasurementType.BATTERY_CHARGE))
        self.assertEqual(registry.providers_for(MeasurementType.BATTERY_CHARGE), [])
        self.assertEqual(registry.as_dict()["supported_measurement_types"], [])


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


class FakeMeasurementProvider:
    provider_name = "Fixture"

    def __init__(
        self,
        provider_id,
        capabilities,
        measurements,
    ):
        self.provider_id = provider_id
        self._capabilities = capabilities
        self._measurements = measurements

    def capabilities(self):
        return list(self._capabilities)

    def measurements(self, start, end):
        start_text = start.isoformat()
        end_text = end.isoformat()
        return [
            measurement
            for measurement in self._measurements
            if start_text <= measurement.timestamp[:10] <= end_text
        ]


if __name__ == "__main__":
    unittest.main()
