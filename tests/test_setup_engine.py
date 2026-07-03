import unittest
from datetime import date, datetime, timedelta, timezone

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
from engine.provider_framework import build_provider_registry


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
