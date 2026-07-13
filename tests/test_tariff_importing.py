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
        self.assertIn("Update detected", markup)
        self.assertIn("Approve tariff update", markup)
        self.assertIn("Dismiss update", markup)
        self.assertIn("View diagnostics", markup)
        self.assertIn("View raw changes", markup)

    def test_tariff_updates_no_update_state_is_plain_language(self):
        active = version()
        markup = report.tariff_updates_html(
            {
                "current_active_tariff": active.as_dict(),
                "pending": [],
                "diagnostics": {
                    "synergy": {
                        "provider": "Synergy",
                        "last_successful_check": "2026-07-12T01:00:00+00:00",
                        "import_status": "no_change",
                    }
                },
            }
        )

        self.assertIn("Synergy EV Add On is current.", markup)
        self.assertIn("Current tariff name", markup)
        self.assertIn("Provider", markup)
        self.assertIn("Effective date", markup)
        self.assertIn("Last checked", markup)
        self.assertIn("Current status", markup)
        self.assertIn("Check for tariff updates", markup)
        self.assertNotIn("Review changes", markup)
        self.assertIn("View diagnostics", markup)
        self.assertIn("<summary>View diagnostics</summary>", markup)

    def test_tariff_update_pending_state_shows_summary_impact_and_decisions(self):
        active = version()
        pending = version(effective_from=date(2026, 8, 1), rate=0.55, supply=1.10, export=0.03)
        update = pending_update(active, pending, {"supply_days": 30, "grid_import_by_bucket": {"peak": 120}, "solar_exported_kwh": 40})

        markup = report.tariff_updates_html({"current_active_tariff": active.as_dict(), "pending": [update], "diagnostics": {}})

        self.assertIn("Update detected", markup)
        self.assertIn("Synergy has published updated rates for your plan.", markup)
        self.assertIn("Based on your recent usage, this change may increase", markup)
        self.assertIn("Estimated annual bill change", markup)
        self.assertIn("Estimated monthly bill change", markup)
        self.assertIn("Usage period used", markup)
        self.assertIn("Review changes", markup)
        self.assertIn("Approve tariff update", markup)
        self.assertIn("Dismiss update", markup)
        self.assertIn("Historical periods will keep the tariff that applied at the time", markup)
        self.assertIn("Approval does not rewrite previous reports", markup)
        self.assertIn("The pending version will be archived", markup)
        self.assertIn("current active tariff remains unchanged", markup)

    def test_tariff_update_comparison_only_shows_changed_fields(self):
        active = version()
        pending = version(effective_from=date(2026, 8, 1), rate=0.55, supply=1.10, export=0.02)
        update = pending_update(active, pending, {"supply_days": 30, "grid_import_by_bucket": {"peak": 120}})

        markup = report.tariff_updates_html({"current_active_tariff": active.as_dict(), "pending": [update], "diagnostics": {}})

        self.assertIn("Daily supply charge", markup)
        self.assertIn("Peak rate", markup)
        self.assertIn("Effective date", markup)
        self.assertNotIn("Off-peak rate", markup)
        self.assertNotIn("Overnight rate", markup)
        self.assertNotIn("Super off-peak rate", markup)

    def test_tariff_update_styles_beneficial_adverse_and_structural_changes(self):
        active = version(export=0.02)
        pending = version(
            effective_from=date(2026, 8, 1),
            rate=0.55,
            supply=1.10,
            export=0.03,
            windows=("Peak: 4pm-9pm", "Off peak: other"),
        )
        update = pending_update(active, pending, {"supply_days": 30, "grid_import_by_bucket": {"peak": 120}, "solar_exported_kwh": 40})

        markup = report.tariff_updates_html({"current_active_tariff": active.as_dict(), "pending": [update], "diagnostics": {}})

        self.assertIn("adverse", markup)
        self.assertIn("cost increase", markup)
        self.assertIn("beneficial", markup)
        self.assertIn("Export credit", markup)
        self.assertIn("neutral", markup)
        self.assertIn("Tariff windows", markup)

    def test_tariff_update_impact_wording_for_unchanged_and_changed_recommendations(self):
        active = version()
        unchanged = version(effective_from=date(2026, 8, 1), rate=0.50, supply=1.00, export=0.02)
        unchanged_update = pending_update(active, unchanged, {"supply_days": 30, "grid_import_by_bucket": {"peak": 120}})
        unchanged_update["financial_preview"]["annual_bill_difference"] = 0.0
        unchanged_update["financial_preview"]["summary"] = "No material impact."
        unchanged_markup = report.tariff_updates_html({"current_active_tariff": active.as_dict(), "pending": [unchanged_update], "diagnostics": {}})

        changed = version(effective_from=date(2026, 8, 1), rate=0.70, supply=1.00, export=0.02)
        changed_update = pending_update(active, changed, {"supply_days": 30, "grid_import_by_bucket": {"peak": 120}})
        changed_markup = report.tariff_updates_html({"current_active_tariff": active.as_dict(), "pending": [changed_update], "diagnostics": {}})

        self.assertIn("The recommended battery size is unchanged.", unchanged_markup)
        self.assertIn("Your current plan still appears to be the better option.", unchanged_markup)
        self.assertIn("Super off-peak charging remains the cheapest window.", unchanged_markup)
        self.assertIn("Battery recommendation may change", changed_markup)
        self.assertIn("Plan comparisons should be reviewed after approval.", changed_markup)

    def test_tariff_update_confirmations_flash_and_diagnostics(self):
        active = version()
        pending = version(effective_from=date(2026, 8, 1), rate=0.55)
        update = pending_update(active, pending, {"supply_days": 30, "grid_import_by_bucket": {"peak": 120}})
        markup = report.tariff_updates_html(
            {"current_active_tariff": active.as_dict(), "pending": [update], "diagnostics": {"synergy": {"provider": "Synergy", "import_status": "pending_review", "last_successful_check": "2026-07-12T00:00:00+00:00", "next_scheduled_check": "2026-07-19"}}},
            {"action": "approve", "status": "success"},
        )

        self.assertIn("Tariff update approved.", markup)
        self.assertIn("data-confirm=\"Approve this tariff update?", markup)
        self.assertIn("data-confirm=\"Dismiss this tariff update?", markup)
        self.assertIn("Provider check", markup)
        self.assertIn("Active version", markup)
        self.assertIn("Pending version", markup)
        self.assertIn("Source and retrieval", markup)
        self.assertIn("Last result", markup)
        self.assertIn("Next scheduled check", markup)
        self.assertIn("tariff-raw-table", markup)

    def test_tariff_update_dismissed_flash_copy(self):
        markup = report.tariff_updates_html(
            {"current_active_tariff": version().as_dict(), "pending": [], "diagnostics": {}},
            {"action": "dismiss", "status": "success"},
        )

        self.assertIn("Tariff update dismissed.", markup)
        self.assertIn("current active tariff remains unchanged", markup)


def pending_update(active: TariffVersion, pending: TariffVersion, usage_profile: dict) -> dict:
    from engine.tariff_importing import pending_update_payload

    return pending_update_payload(
        active,
        pending,
        tariff_content_checksum(pending),
        tariff_version_diff(active, pending),
        usage_profile=usage_profile,
        detected_at="2026-07-12T00:00:00+00:00",
    )


if __name__ == "__main__":
    unittest.main()
