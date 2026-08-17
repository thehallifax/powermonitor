import sqlite3
from contextlib import closing
import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import report
from engine.financial_engine import FinancialEngine
from engine.tariff_versioning import (
    TariffResolver,
    TariffVersion,
    TariffVersionImmutableError,
    TariffVersionStore,
    default_tariff_store,
    resolver_for_settings,
)


def version(
    effective_from: date,
    *,
    name: str = "Synergy EV Add On",
    provider: str = "Synergy",
    identifier: str = None,
    rate: float = 0.50,
    supply: float = 1.0,
    export: float = 0.02,
):
    return TariffVersion.create(
        provider=provider,
        tariff_name=name,
        version=identifier or effective_from.isoformat(),
        effective_from=effective_from,
        effective_to=None,
        supply_charge_per_day=supply,
        usage_rates={"peak": rate, "off_peak": rate / 2, "overnight": rate / 3, "super_off_peak": rate / 4},
        export_rates={"standard": export},
        tariff_windows=("Peak: evening",),
        currency="AUD",
        source_identifier="unit-test",
        created_at=f"{effective_from.isoformat()}T00:00:00+00:00",
    )


class TariffVersioningTests(unittest.TestCase):
    def test_tariff_version_creation_has_required_metadata_and_hash(self):
        tariff = version(date(2026, 7, 1), identifier="v2026")

        self.assertEqual(tariff.provider, "Synergy")
        self.assertEqual(tariff.tariff_name, "Synergy EV Add On")
        self.assertEqual(tariff.version, "v2026")
        self.assertEqual(tariff.effective_from, date(2026, 7, 1))
        self.assertIsNone(tariff.effective_to)
        self.assertEqual(tariff.currency, "AUD")
        self.assertEqual(len(tariff.version_hash), 64)
        self.assertIn("version_id", tariff.as_dict())

    def test_tariff_version_is_immutable(self):
        tariff = version(date(2026, 7, 1))

        with self.assertRaises(FrozenInstanceError):
            tariff.supply_charge_per_day = 2.0

        self.assertAlmostEqual(tariff.usage_rate("peak"), 0.50)

    def test_replacement_closes_previous_version_without_mutating_original(self):
        first = version(date(2026, 1, 1), identifier="v1", rate=0.40)
        second = version(date(2026, 7, 1), identifier="v2", rate=0.60)
        store = TariffVersionStore([first])

        store.add_version(second)
        stored_first = store.versions("Synergy", "Synergy EV Add On")[0]

        self.assertIsNone(first.effective_to)
        self.assertEqual(stored_first.effective_to, date(2026, 6, 30))
        self.assertAlmostEqual(stored_first.usage_rate("peak"), 0.40)
        self.assertAlmostEqual(store.versions("Synergy", "Synergy EV Add On")[1].usage_rate("peak"), 0.60)

    def test_duplicate_version_identity_is_rejected(self):
        tariff = version(date(2026, 7, 1), identifier="same")
        store = TariffVersionStore([tariff])

        with self.assertRaises(TariffVersionImmutableError):
            store.add_version(tariff)

    def test_resolver_returns_historical_current_and_future_versions(self):
        store = TariffVersionStore([
            version(date(2026, 1, 1), identifier="old", rate=0.40),
            version(date(2026, 7, 1), identifier="new", rate=0.60),
        ])
        resolver = TariffResolver(store, default_provider="Synergy", default_tariff_name="Synergy EV Add On")

        self.assertEqual(resolver.resolve(date(2026, 3, 1)).version, "old")
        self.assertEqual(resolver.resolve(date(2026, 7, 1)).version, "new")
        self.assertEqual(resolver.resolve(date(2027, 1, 1)).version, "new")
        self.assertIsNone(resolver.resolve(date(2027, 1, 1)).effective_to)

    def test_resolver_supports_multiple_providers_and_catalogues(self):
        store = TariffVersionStore([
            version(date(2026, 7, 1), provider="Synergy", name="Home", identifier="synergy"),
            version(date(2026, 7, 1), provider="Other Retailer", name="Home", identifier="other", rate=0.90),
        ])
        resolver = TariffResolver(store)

        self.assertEqual(
            resolver.resolve(date(2026, 8, 1), provider="Other Retailer", tariff_name="Home").provider,
            "Other Retailer",
        )

    def test_default_catalog_resolver_exposes_current_open_ended_version(self):
        settings = SimpleNamespace(current_plan="synergy_ev_add_on", retailer="Synergy")
        tariff = resolver_for_settings(settings).resolve(date(2026, 7, 12))

        self.assertEqual(tariff.provider, "Synergy")
        self.assertEqual(tariff.tariff_name, "Synergy EV Add On")
        self.assertEqual(tariff.effective_from, date(2026, 7, 1))
        self.assertIsNone(tariff.effective_to)

    def test_financial_engine_battery_payload_contains_tariff_metadata(self):
        tariff = version(date(2026, 7, 1), identifier="battery", rate=0.60)
        result = FinancialEngine().compare_home_battery_sizes(
            [
                {"measurement_time": "2026-07-02T12:00:00", "tariff_bucket": "super_off_peak", "solar_export_kwh": 5.0},
                {"measurement_time": "2026-07-02T18:00:00", "tariff_bucket": "peak", "grid_import_kwh": 5.0},
            ],
            SimpleNamespace(import_rates=tariff.usage_rate_map, export_credit_rate=tariff.primary_export_rate),
            baseline_bill=20.0,
            baseline_grid_import_kwh=5.0,
            baseline_solar_exported_kwh=5.0,
            tariff_version=tariff,
        )

        self.assertEqual(result["tariff_version"], tariff.version_id)
        self.assertEqual(result["tariff_effective_from"], "2026-07-01")
        self.assertEqual(result["tariff_name"], "Synergy EV Add On")

    def test_report_bill_calculation_uses_historical_tariff_by_reading_date(self):
        settings = SimpleNamespace(
            current_plan="synergy_ev_add_on",
            retailer="Synergy",
            supply_charge_per_day=99.0,
            export_credit_rate=0.99,
            ev_baseline_watts=0.0,
            ev_data_source="estimated",
            import_rates={"super_off_peak": 9.0, "overnight": 9.0, "off_peak": 9.0, "peak": 9.0},
        )
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            create_readings(conn)
            report.data_engine(conn).validate_database()
            summary = report.load_summary(conn, date(2026, 6, 30), date(2026, 7, 1), settings)

        catalog = default_tariff_store(settings)
        historical = TariffResolver(catalog, default_provider="Synergy", default_tariff_name="Synergy EV Add On").resolve(date(2026, 6, 30))
        current = TariffResolver(catalog, default_provider="Synergy", default_tariff_name="Synergy EV Add On").resolve(date(2026, 7, 1))
        expected_import = historical.usage_rate("peak") + current.usage_rate("peak")
        expected_supply = historical.supply_charge_per_day + current.supply_charge_per_day
        expected_credit = historical.primary_export_rate + current.primary_export_rate

        self.assertAlmostEqual(summary.import_cost, expected_import)
        self.assertAlmostEqual(summary.supply_cost, expected_supply)
        self.assertAlmostEqual(summary.export_credit, expected_credit)
        self.assertAlmostEqual(summary.net_cost, expected_import + expected_supply - expected_credit)

    def test_dashboard_api_exposes_current_tariff_metadata(self):
        data = report.dashboard_data(today=date(2026, 7, 12), period_kind="today")

        self.assertIn("current_tariff", data)
        self.assertEqual(data["current_tariff"]["provider"], "Synergy")
        self.assertEqual(data["current_tariff"]["tariff_name"], "Synergy EV Add On")
        self.assertEqual(data["current_tariff"]["effective_from"], "2026-07-01")
        self.assertIn("battery_comparison", data)
        self.assertIn("tariff_version", data["battery_comparison"])
        self.assertIn("tariff_effective_from", data["battery_comparison"])


def create_readings(conn) -> None:
    conn.execute(
        """
        CREATE TABLE readings (
            household_id TEXT DEFAULT 'default_household',
            measurement_time TEXT,
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
    conn.executemany(
        """
        INSERT INTO readings (
            household_id,
            measurement_time,
            tariff_bucket,
            production_kwh,
            solar_to_home_kwh,
            solar_to_grid_kwh,
            grid_to_home_w,
            grid_to_home_kwh,
            import_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("default_household", "2026-06-30T18:00:00", "peak", 0.0, 0.0, 1.0, 4000.0, 1.0, 0.0),
            ("default_household", "2026-07-01T18:00:00", "peak", 0.0, 0.0, 1.0, 4000.0, 1.0, 0.0),
        ],
    )


if __name__ == "__main__":
    unittest.main()
