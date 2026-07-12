import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import report
from engine.tariff_importing import (
    ImportedTariff,
    SynergyTariffProvider,
    TariffImportManager,
    TariffProviderRegistry,
    TariffUpdateRepository,
    financial_impact_preview,
    tariff_content_checksum,
    tariff_version_diff,
)
from engine.tariff_versioning import TariffResolver, TariffVersion, TariffVersionStore, default_tariff_store


def version(
    *,
    effective_from=date(2026, 7, 1),
    rate=0.50,
    supply=1.00,
    export=0.02,
    windows=("Peak: evening", "Off peak: other"),
):
    return TariffVersion.create(
        provider="Synergy",
        tariff_name="Synergy EV Add On",
        version=effective_from.isoformat(),
        effective_from=effective_from,
        effective_to=None,
        supply_charge_per_day=supply,
        usage_rates={"peak": rate, "off_peak": 0.25, "overnight": 0.15, "super_off_peak": 0.08},
        export_rates={"standard": export},
        tariff_windows=windows,
        currency="AUD",
        source_identifier="unit-test",
        created_at=f"{effective_from.isoformat()}T00:00:00+00:00",
        gst_included=True,
    )


def imported_from(tariff: TariffVersion, **overrides):
    data = {
        "provider": tariff.provider,
        "tariff_name": tariff.tariff_name,
        "effective_from": tariff.effective_from,
        "supply_charge_per_day": tariff.supply_charge_per_day,
        "usage_rates": tariff.usage_rate_map,
        "export_rates": tariff.export_rate_map,
        "tariff_windows": tariff.tariff_windows,
        "currency": tariff.currency,
        "source_identifier": "unit-import",
        "source_url": "https://example.test/tariffs",
        "retrieved_at": "2026-07-12T00:00:00+00:00",
        "gst_included": tariff.gst_included,
    }
    data.update(overrides)
    imported = ImportedTariff(**data)
    return ImportedTariff(**{**imported.__dict__, "checksum": imported.calculate_checksum()})


class FakeProvider:
    def __init__(self, tariffs):
        self.tariffs = tariffs

    def provider_name(self):
        return "Synergy"

    def supported_tariffs(self):
        return ["Synergy EV Add On"]

    def fetch_current_tariffs(self):
        return self.tariffs


class TariffImportingTests(unittest.TestCase):
    def manager(self, imported, active=None, state=None):
        active = active or version()
        repository = TariffUpdateRepository(state=state if state is not None else {})
        return TariffImportManager(
            registry=TariffProviderRegistry([FakeProvider(imported)]),
            active_store=TariffVersionStore([active]),
            repository=repository,
            clock=lambda: datetime(2026, 7, 12, tzinfo=timezone.utc),
        ), repository

    def test_provider_registration_supports_future_retailers_and_rejects_duplicates(self):
        registry = TariffProviderRegistry([FakeProvider([])])

        self.assertEqual(registry.provider("Synergy").provider_name(), "Synergy")
        with self.assertRaises(ValueError):
            registry.register(FakeProvider([]))

    def test_synergy_provider_fetches_structured_tariffs_from_loader(self):
        provider = SynergyTariffProvider(
            source_url="https://example.test/synergy.json",
            source_loader=lambda _url: {
                "tariffs": [
                    {
                        "tariff_name": "Synergy EV Add On",
                        "effective_from": "2026-08-01",
                        "supply_charge_per_day": 1.29,
                        "usage_rates": {"peak": 0.60, "off_peak": 0.23, "overnight": 0.19, "super_off_peak": 0.08},
                        "export_rates": {"standard": 0.03},
                        "tariff_windows": ["Peak: 3pm-9pm"],
                        "gst_included": True,
                    }
                ]
            },
            clock=lambda: datetime(2026, 7, 12, tzinfo=timezone.utc),
        )

        tariffs = provider.fetch_current_tariffs()

        self.assertEqual(tariffs[0].provider, "Synergy")
        self.assertEqual(tariffs[0].tariff_name, "Synergy EV Add On")
        self.assertEqual(tariffs[0].source_url, "https://example.test/synergy.json")
        self.assertEqual(len(tariffs[0].checksum), 64)

    def test_no_tariff_changes_do_not_create_pending_update(self):
        active = version()
        manager, repository = self.manager([imported_from(active)], active=active)

        result = manager.check_for_updates("Synergy")

        self.assertEqual(result["status"], "no_change")
        self.assertEqual(repository.pending_updates(), [])

    def test_single_rate_change_creates_pending_update_with_human_diff(self):
        active = version()
        imported = imported_from(active, usage_rates={**active.usage_rate_map, "peak": 0.607})
        manager, repository = self.manager([imported], active=active)

        result = manager.check_for_updates("Synergy", usage_profile={"supply_days": 10, "grid_import_by_bucket": {"peak": 100}, "solar_exported_kwh": 0})
        pending = repository.pending_updates()[0]

        self.assertEqual(result["status"], "pending_review")
        self.assertIn("Peak", pending["diff"][0]["label"])
        self.assertIn("50.0 c", pending["diff"][0]["before"])
        self.assertIn("60.7 c", pending["diff"][0]["after"])
        self.assertTrue(pending["financial_preview"]["available"])

    def test_tariff_window_supply_and_export_changes_are_reported(self):
        active = version()
        pending = version(supply=1.29, export=0.03, windows=("Peak: 4pm-8pm",), rate=0.50)
        diff = tariff_version_diff(active, pending)
        labels = [item["label"] for item in diff]

        self.assertIn("Supply charge", labels)
        self.assertIn("Export", labels)
        self.assertIn("Tariff windows", labels)

    def test_checksum_changes_when_financial_content_changes(self):
        active = version()
        pending = version(rate=0.51)

        self.assertNotEqual(tariff_content_checksum(active), tariff_content_checksum(pending))

    def test_approval_preserves_historical_version_and_activates_future_version(self):
        active = version(effective_from=date(2026, 7, 1), rate=0.50)
        imported = imported_from(active, effective_from=date(2026, 8, 1), usage_rates={**active.usage_rate_map, "peak": 0.60})
        manager, repository = self.manager([imported], active=active)
        manager.check_for_updates("Synergy")
        update_id = repository.pending_updates()[0]["update_id"]

        repository.approve(update_id, approved_at="2026-07-12T01:00:00+00:00")
        settings = SimpleNamespace(current_plan="synergy_ev_add_on", retailer="Synergy", approved_tariff_versions=repository.approved_version_dicts())
        store = default_tariff_store(settings)
        resolver = TariffResolver(store, default_provider="Synergy", default_tariff_name="Synergy EV Add On")

        self.assertAlmostEqual(resolver.resolve(date(2026, 7, 15)).usage_rate("peak"), 0.538446)
        self.assertAlmostEqual(resolver.resolve(date(2026, 8, 1)).usage_rate("peak"), 0.60)
        self.assertEqual(repository.pending_updates(), [])

    def test_dismissal_archives_pending_without_approval(self):
        active = version()
        imported = imported_from(active, usage_rates={**active.usage_rate_map, "peak": 0.60})
        manager, repository = self.manager([imported], active=active)
        manager.check_for_updates("Synergy")
        update_id = repository.pending_updates()[0]["update_id"]

        archived = repository.dismiss(update_id, dismissed_at="2026-07-12T01:00:00+00:00")

        self.assertEqual(archived["status"], "dismissed")
        self.assertEqual(repository.pending_updates(), [])
        self.assertEqual(repository.approved_version_dicts(), [])

    def test_financial_preview_annualises_recent_usage(self):
        current = version(rate=0.50, supply=1.00, export=0.02)
        pending = version(rate=0.60, supply=1.10, export=0.03)

        preview = financial_impact_preview(
            current,
            pending,
            {"supply_days": 10, "grid_import_by_bucket": {"peak": 100}, "solar_exported_kwh": 20},
        )

        self.assertTrue(preview["available"])
        self.assertEqual(preview["estimated_change"], "increase")
        self.assertAlmostEqual(preview["annual_bill_difference"], ((10.0 + 1.0 - 0.2) / 10) * 365)
        self.assertIn("previous 10 days", preview["summary"])

    def test_diagnostics_are_recorded_and_rendered(self):
        active = version()
        imported = imported_from(active, usage_rates={**active.usage_rate_map, "peak": 0.60})
        manager, repository = self.manager([imported], active=active)

        result = manager.check_for_updates("Synergy")
        markup = report.tariff_updates_html({
            "current_active_tariff": active.as_dict(),
            "pending": repository.pending_updates(),
            "diagnostics": repository.diagnostics(),
        })

        self.assertEqual(result["diagnostics"]["import_status"], "pending_review")
        self.assertIn("Tariff updates", markup)
        self.assertIn("Pending tariff update", markup)
        self.assertIn("Approve update", markup)
        self.assertIn("Dismiss update", markup)
        self.assertIn("Tariff import diagnostics", markup)
        self.assertIn("View raw changes", markup)


if __name__ == "__main__":
    unittest.main()
