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


if __name__ == "__main__":
    unittest.main()
