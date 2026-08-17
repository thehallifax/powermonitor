import unittest
from datetime import date, datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from pathlib import Path

from engine.import_framework import ImportSource
from engine.models import (
    Account,
    CapabilityStatus,
    Freshness,
    Household,
    HouseholdCapability,
    HouseholdCapabilitySummary,
    HouseholdContext,
    HouseholdMembership,
)
from engine.setup_engine import SetupEngine
from engine.provider_framework import Provider, ProviderCapability, ProviderRegistry, ProviderStatus, ProviderType, build_provider_registry
from engine.provider_manager import ProviderManager


class SetupEngineTests(unittest.TestCase):
    def test_setup_tasks_respect_household_capabilities(self):
        checklist = SetupEngine(context_with("solar")).build_checklist(
            capability_summary(
                solar=False,
                ev=False,
                tariff=False,
                latest=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
            ),
            collector_health={"status": "ok"},
            import_sources=[ImportSource("ocular", "Ocular", (".xlsx",))],
            settings=settings(billing_configured=True),
            now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
        )

        task_ids = {task.task_id for task in checklist.tasks}
        self.assertIn("connect_solar_provider", task_ids)
        self.assertNotIn("upload_ev_charging_history", task_ids)
        self.assertNotIn("connect_battery_provider", task_ids)

    def test_incomplete_tariff_collector_and_stale_data_are_high_priority(self):
        checklist = SetupEngine(context_with("ev")).build_checklist(
            capability_summary(
                solar=False,
                ev=False,
                tariff=False,
                latest=datetime(2026, 6, 26, 8, 0, tzinfo=timezone.utc),
            ),
            collector_health={"status": "stale", "message": "Collection has not completed today."},
            import_sources=[ImportSource("ocular", "Ocular", (".xlsx", ".xlsm"))],
            settings=settings(billing_configured=False),
            now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
        )

        remaining = {task.task_id: task for task in checklist.progress.remaining_tasks}
        self.assertEqual(remaining["configure_tariff"].priority.value, "high")
        self.assertEqual(remaining["collector_offline"].priority.value, "high")
        self.assertEqual(remaining["data_older_than_24_hours"].priority.value, "high")
        self.assertIn(".xlsx", remaining["upload_ev_charging_history"].rationale)
        self.assertLess(checklist.progress.completion_pct, 100)

    def test_setup_tasks_can_include_provider_registry_metadata(self):
        summary = capability_summary(
            solar=False,
            ev=False,
            tariff=True,
            latest=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
        )
        registry = build_provider_registry(
            settings=type("Settings", (), {"ev_data_source": "estimated"})(),
            health={"status": "ok"},
            capability_summary=summary,
            import_sources=[ImportSource("ocular_ev_export", "Ocular EV export", (".xlsx",))],
        )

        checklist = SetupEngine(context_with("solar", "ev")).build_checklist(
            summary,
            collector_health={"status": "ok"},
            import_sources=[ImportSource("ocular_ev_export", "Ocular EV export", (".xlsx",))],
            settings=settings(billing_configured=True),
            provider_registry=registry,
            now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
        )

        tasks = {task.task_id: task for task in checklist.tasks}

        self.assertIn("SolarEdge", tasks["connect_solar_provider"].rationale)
        self.assertEqual(tasks["connect_solar_provider"].action_href, "#system-provider-registry-solaredge")
        self.assertIn("Ocular export", tasks["upload_ev_charging_history"].rationale)

    def test_completed_setup_reaches_full_progress(self):
        checklist = SetupEngine(context_with("solar", "ev")).build_checklist(
            capability_summary(
                solar=True,
                ev=True,
                tariff=True,
                latest=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
            ),
            collector_health={"status": "ok"},
            import_sources=[ImportSource("ocular", "Ocular", (".xlsx",))],
            goals=[object()],
            recommendations=[object()],
            settings=settings(billing_configured=True),
            now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
        )

        self.assertTrue(checklist.progress.complete)
        self.assertEqual(checklist.progress.completion_pct, 100)
        self.assertEqual(checklist.progress.remaining_tasks, [])

    def test_billing_cycle_task_points_to_configuration_page(self):
        checklist = SetupEngine(context_with("solar", "ev")).build_checklist(
            capability_summary(
                solar=True,
                ev=True,
                tariff=True,
                latest=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
            ),
            collector_health={"status": "ok"},
            settings=settings(billing_configured=False),
            now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
        )

        tasks = {task.task_id: task for task in checklist.tasks}
        self.assertEqual(tasks["configure_billing_cycle"].action_href, "#setup-billing")
        self.assertEqual(tasks["configure_billing_cycle"].action_label, "Set Billing Cycle")

    def test_manual_tariff_selection_completes_tariff_readiness(self):
        manual_settings = settings(billing_configured=True)
        manual_settings.tariff_setup_mode = "manual"
        manual_settings.current_plan = "manual_tariff"
        checklist = SetupEngine(context_with("solar")).build_checklist(
            capability_summary(
                solar=True,
                ev=False,
                tariff=False,
                latest=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
            ),
            collector_health={"status": "ok"},
            settings=manual_settings,
            now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
        )

        tasks = {task.task_id: task for task in checklist.tasks}
        self.assertEqual(tasks["configure_tariff"].status, "complete")
        self.assertEqual(tasks["configure_tariff"].source, "settings.tariff")

    def test_setup_tasks_point_to_real_configuration_locations(self):
        checklist = SetupEngine(context_with("ev")).build_checklist(
            capability_summary(
                solar=False,
                ev=False,
                tariff=False,
                latest=datetime(2026, 6, 26, 8, 0, tzinfo=timezone.utc),
            ),
            collector_health={"status": "stale", "message": "Collection has not completed today."},
            import_sources=[ImportSource("ocular", "Ocular", (".xlsx", ".xlsm"))],
            settings=settings(billing_configured=False),
            now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
        )

        tasks = {task.task_id: task for task in checklist.tasks}

        self.assertEqual(tasks["configure_tariff"].action_href, "#setup-tariff")
        self.assertEqual(tasks["configure_tariff"].action_label, "Choose Plan")
        self.assertEqual(tasks["upload_ev_charging_history"].action_href, "#imports")
        self.assertEqual(tasks["upload_ev_charging_history"].action_label, "Add EV History")
        self.assertEqual(tasks["collector_offline"].action_href, "#system-live-data")
        self.assertEqual(tasks["collector_offline"].action_label, "Review Live Data")
        self.assertEqual(tasks["data_older_than_24_hours"].action_href, "#system-data-quality")
        self.assertEqual(tasks["data_older_than_24_hours"].action_label, "Can I trust today's advice?")
        self.assertEqual(tasks["review_energy_score"].action_href, "#energy-score-explanation")
        self.assertEqual(tasks["review_energy_score"].action_label, "Why is my score what it is?")
        self.assertNotIn("explore_recommendation_centre", tasks)
        self.assertEqual(tasks["configure_household_goals"].action_href, "#home-goals")
        self.assertNotIn("enable_notifications", tasks)

    def test_solar_provider_selected_without_credentials_prompts_connection(self):
        with TemporaryDirectory() as directory:
            manager = ProviderManager(solar_registry(), **manager_paths(directory))
            checklist = SetupEngine(context_with("solar")).build_checklist(
                capability_summary(
                    solar=False,
                    ev=False,
                    tariff=True,
                    latest=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
                ),
                collector_health={"status": "ok"},
                settings=settings(billing_configured=True),
                provider_management=manager,
                solar_data_available=False,
                now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
            )

        solar_task = task_by_id(checklist, "connect_solar_provider")
        self.assertEqual(solar_task.status, "action_needed")
        self.assertEqual(solar_task.action_label, "Connect SolarEdge")
        self.assertIn("connection details are missing", solar_task.rationale)

    def test_configured_solar_provider_without_readings_prompts_collection(self):
        with TemporaryDirectory() as directory:
            manager = ProviderManager(solar_registry(), **manager_paths(directory))
            manager.action("solaredge", "save", {"cookie": "redaction-fixture-cookie"})
            checklist = SetupEngine(context_with("solar")).build_checklist(
                capability_summary(
                    solar=False,
                    ev=False,
                    tariff=True,
                    latest=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
                ),
                collector_health={"status": "ok"},
                settings=settings(billing_configured=True),
                provider_management=manager,
                solar_data_available=False,
                now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
            )

        solar_task = task_by_id(checklist, "connect_solar_provider")
        self.assertEqual(solar_task.status, "action_needed")
        self.assertEqual(solar_task.action_label, "Collect Solar Data")
        self.assertIn("has not collected usable solar generation readings", solar_task.rationale)

    def test_verified_solar_readings_complete_add_solar_data_task(self):
        manager = ProviderManager(solar_registry())
        checklist = SetupEngine(context_with("solar")).build_checklist(
            capability_summary(
                solar=True,
                ev=False,
                tariff=True,
                latest=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
            ),
            collector_health={"status": "ok"},
            settings=settings(billing_configured=True),
            provider_management=manager,
            solar_data_available=True,
            now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
        )

        solar_task = task_by_id(checklist, "connect_solar_provider")
        self.assertEqual(solar_task.status, "complete")
        self.assertEqual(solar_task.action_label, "Review Solar Data")
        self.assertIn("Verified normalized solar generation readings", solar_task.rationale)

    def test_provider_warning_with_historical_readings_keeps_solar_task_complete(self):
        with TemporaryDirectory() as directory:
            manager = ProviderManager(
                solar_registry(),
                connection_testers={
                    "solaredge": lambda: {
                        "success": False,
                        "authentication_status": "not_authenticated",
                        "warnings": ["Sign-in failed."],
                    }
                },
                **manager_paths(directory),
            )
            manager.action("solaredge", "save", {"username": "user@example.com", "password": "redaction-fixture-password"})
            manager.action("solaredge", "test")
            checklist = SetupEngine(context_with("solar")).build_checklist(
                capability_summary(
                    solar=True,
                    ev=False,
                    tariff=True,
                    latest=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
                ),
                collector_health={"status": "ok"},
                settings=settings(billing_configured=True),
                provider_management=manager,
                solar_data_available=True,
                now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
            )

        solar_task = task_by_id(checklist, "connect_solar_provider")
        self.assertEqual(solar_task.status, "complete")
        self.assertIn("Verified normalized solar generation readings", solar_task.rationale)

    def test_disabled_provider_with_existing_readings_keeps_solar_task_complete(self):
        with TemporaryDirectory() as directory:
            manager = ProviderManager(solar_registry(), **manager_paths(directory))
            manager.action("solaredge", "save", {"cookie": "redaction-fixture-cookie"})
            manager.action("solaredge", "disable")
            checklist = SetupEngine(context_with("solar")).build_checklist(
                capability_summary(
                    solar=True,
                    ev=False,
                    tariff=True,
                    latest=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
                ),
                collector_health={"status": "ok"},
                settings=settings(billing_configured=True),
                provider_management=manager,
                solar_data_available=True,
                now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
            )

        solar_task = task_by_id(checklist, "connect_solar_provider")
        self.assertEqual(solar_task.status, "complete")

    def test_setup_progress_and_next_task_follow_solar_data_availability(self):
        incomplete = SetupEngine(context_with("solar")).build_checklist(
            capability_summary(
                solar=False,
                ev=False,
                tariff=True,
                latest=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
            ),
            collector_health={"status": "ok"},
            goals=[object()],
            settings=settings(billing_configured=True),
            provider_management=ProviderManager(solar_registry()),
            solar_data_available=False,
            now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
        )
        complete = SetupEngine(context_with("solar")).build_checklist(
            capability_summary(
                solar=True,
                ev=False,
                tariff=True,
                latest=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
            ),
            collector_health={"status": "ok"},
            goals=[object()],
            settings=settings(billing_configured=True),
            provider_management=ProviderManager(solar_registry()),
            solar_data_available=True,
            now=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc),
        )

        self.assertLess(incomplete.progress.completion_pct, complete.progress.completion_pct)
        self.assertEqual(incomplete.progress.remaining_tasks[0].task_id, "connect_solar_provider")
        self.assertNotIn("connect_solar_provider", [task.task_id for task in complete.progress.remaining_tasks])


def context_with(*capabilities: str) -> HouseholdContext:
    return HouseholdContext(
        account=Account("acct", "Local account", "local"),
        household=Household(
            "household",
            "Home",
            "Australia/Perth",
            [HouseholdCapability(capability, source="test") for capability in capabilities],
        ),
        membership=HouseholdMembership("acct", "household", "owner"),
    )


def task_by_id(checklist, task_id: str):
    return next(task for task in checklist.tasks if task.task_id == task_id)


def solar_registry() -> ProviderRegistry:
    return ProviderRegistry(
        [
            Provider(
                provider_id="solaredge",
                name="SolarEdge",
                provider_type=ProviderType.SOLAR,
                capabilities=[ProviderCapability.INTERVAL_HISTORY, ProviderCapability.DAILY_SUMMARY],
                status=ProviderStatus.AVAILABLE,
            )
        ]
    )


def manager_paths(directory):
    root = Path(directory)
    return {
        "provider_state_path": root / "provider_state.json",
        "entity_mapping_store_path": root / "provider_mappings.json",
        "secrets_path": root / "secrets.json",
        "settings_path": root / "settings.json",
        "solaredge_cookie_path": root / "solaredge_cookie.txt",
    }


def capability_summary(
    *,
    solar: bool,
    ev: bool,
    tariff: bool,
    latest: datetime,
) -> HouseholdCapabilitySummary:
    observed_at = latest.isoformat(timespec="seconds")
    freshness = Freshness(
        state="fresh" if latest.date() >= date(2026, 6, 28) else "stale",
        observed_at=observed_at,
    )
    return HouseholdCapabilitySummary(
        household_id="household",
        generated_at=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc).isoformat(timespec="seconds"),
        capabilities=[
            CapabilityStatus("solar", "Solar", present=solar, reason="Solar state", freshness=freshness if solar else None),
            CapabilityStatus("ev", "EV charging", present=ev, available=True, reason="EV state"),
            CapabilityStatus("tariff", "Tariff", configured=tariff, reason="Tariff state"),
            CapabilityStatus("imports", "Imports", available=True, reason="Imports available"),
            CapabilityStatus("live_local_collector", "Live/local collector", present=True, reason="Collector state"),
            CapabilityStatus("data_freshness", "Data freshness", present=True, reason="Freshness state", freshness=freshness),
        ],
    )


class settings:
    def __init__(self, billing_configured: bool) -> None:
        self.current_billing_period_start = date(2026, 6, 1) if billing_configured else None
        self.current_billing_period_end = date(2026, 6, 30) if billing_configured else None
        self.billing_cycle_configured = billing_configured
        self.billing_period_start_day = 1
        self.retailer = "Synergy"
        self.retailer_id = "synergy"
        self.state_or_territory = "WA"
        self.tariff_setup_mode = "catalog"
        self.tariff_add_on = "ev_add_on"
        self.current_plan = "synergy_ev_add_on"


if __name__ == "__main__":
    unittest.main()
