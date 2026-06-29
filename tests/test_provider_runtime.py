import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from engine.provider_framework import ProviderCapability, ProviderObservation, ProviderObservationQuality, ProviderType
from engine.provider_runtime import (
    ProviderAuthentication,
    ProviderExecutionState,
    ProviderRuntime,
    ProviderSchedule,
)


LOCAL = ZoneInfo("Australia/Perth")


class FakeAdapter:
    provider_id = "fake_provider"
    schedule = ProviderSchedule.HOURLY

    def __init__(self, *, fail_times=0, auth_refresh=False):
        self.fail_times = fail_times
        self.auth_refresh = auth_refresh
        self.calls = 0
        self.refresh_calls = 0

    def authenticate(self):
        if self.auth_refresh and not self.refresh_calls:
            return ProviderAuthentication(authenticated=False, refresh_required=True, method="fixture")
        return ProviderAuthentication(authenticated=True, method="fixture")

    def refresh_session(self):
        self.refresh_calls += 1
        return ProviderAuthentication(authenticated=True, method="fixture")

    def test_connection(self):
        return {"success": True}

    def discover_capabilities(self):
        return [ProviderCapability.DAILY_SUMMARY]

    def collect_observations(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("planned failure")
        return [
            ProviderObservation(
                capability=ProviderCapability.DAILY_SUMMARY,
                name="fixture",
                value={"ok": True},
                provider_id=self.provider_id,
                provider_type=ProviderType.GENERIC_IMPORT,
                quality=ProviderObservationQuality.HIGH,
            )
        ]

    def collect_live_state(self):
        return {"ok": True}

    def collect_diagnostics(self):
        return {"calls": self.calls}


class FakeDataEngine:
    def __init__(self):
        self.persisted = []

    def persist_provider_observations(self, observations):
        self.persisted.extend(observations)
        return [{"provider_id": item.provider_id} for item in observations]


class ProviderRuntimeTests(unittest.TestCase):
    def test_runtime_execution_submits_observations_and_updates_health(self):
        engine = FakeDataEngine()
        runtime = ProviderRuntime([FakeAdapter()], data_engine=engine)

        result = runtime.run_provider("fake_provider")

        self.assertTrue(result.success)
        self.assertEqual(result.state, ProviderExecutionState.SUCCESS)
        self.assertEqual(result.metrics.observations_submitted, 1)
        self.assertEqual(len(engine.persisted), 1)
        self.assertEqual(runtime.health["fake_provider"].consecutive_failures, 0)

    def test_scheduler_respects_due_times(self):
        now = datetime(2026, 6, 29, 10, tzinfo=LOCAL)
        runtime = ProviderRuntime([FakeAdapter()], now=now)

        self.assertFalse(runtime.is_due("fake_provider", now + timedelta(minutes=30)))
        self.assertTrue(runtime.is_due("fake_provider", now + timedelta(hours=1, seconds=1)))

    def test_retry_logic_recovers_before_failure(self):
        adapter = FakeAdapter(fail_times=1)
        runtime = ProviderRuntime([adapter], max_retries=1)

        result = runtime.run_provider("fake_provider")

        self.assertTrue(result.success)
        self.assertEqual(result.metrics.retry_count, 1)
        self.assertEqual(adapter.calls, 2)

    def test_runtime_health_tracks_backoff_after_failures(self):
        runtime = ProviderRuntime([FakeAdapter(fail_times=3)], max_retries=1, base_backoff_seconds=5)

        result = runtime.run_provider("fake_provider")
        health = runtime.health["fake_provider"]

        self.assertFalse(result.success)
        self.assertEqual(health.state, ProviderExecutionState.BACKING_OFF)
        self.assertEqual(health.consecutive_failures, 1)
        self.assertIsNotNone(health.next_run_at)

    def test_authentication_refresh_happens_before_collection(self):
        adapter = FakeAdapter(auth_refresh=True)
        runtime = ProviderRuntime([adapter])

        result = runtime.run_provider("fake_provider")

        self.assertTrue(result.success)
        self.assertEqual(adapter.refresh_calls, 1)

    def test_runtime_serializes_provider_status_without_secrets(self):
        runtime = ProviderRuntime([FakeAdapter()])
        data = runtime.as_dict()

        self.assertEqual(data["runtime_health"], "healthy")
        self.assertEqual(data["active_providers"], ["fake_provider"])
        self.assertIn("providers", data)


if __name__ == "__main__":
    unittest.main()
