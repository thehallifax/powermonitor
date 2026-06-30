import unittest

from engine.provider_framework import (
    Provider,
    ProviderCapability,
    ProviderRegistry,
    ProviderStatus,
    ProviderType,
)
from engine.provider_manager import (
    ProviderConnectionStatus,
    ProviderManager,
    ProviderOnboardingState,
)


def registry(status=ProviderStatus.AVAILABLE):
    return ProviderRegistry(
        [
            Provider(
                provider_id="solaredge",
                name="SolarEdge",
                provider_type=ProviderType.SOLAR,
                capabilities=[ProviderCapability.INTERVAL_HISTORY, ProviderCapability.DAILY_SUMMARY],
                status=status,
                note="Solar provider.",
            ),
            Provider(
                provider_id="ocular_export",
                name="Ocular export",
                provider_type=ProviderType.EV_CHARGER,
                capabilities=[ProviderCapability.EV_SESSIONS, ProviderCapability.IMPORT_ONLY],
                status=ProviderStatus.SUPPORTED,
            ),
        ]
    )


class ProviderManagerTests(unittest.TestCase):
    def test_configuration_lifecycle_and_state_transitions(self):
        manager = ProviderManager(registry())
        provider = manager.get("solaredge")

        self.assertEqual(provider.lifecycle_state, ProviderConnectionStatus.CONNECTED)
        self.assertIn("Reconnect", provider.actions)
        self.assertIn("Remove", provider.actions)

    def test_credential_updates_are_redacted(self):
        manager = ProviderManager(registry(status=ProviderStatus.MISSING))
        configuration = manager.configure(
            "solaredge",
            {
                "username": "user@example.com",
                "password": "redaction-fixture-one",
                "cookie": "redaction-fixture-two",
                "endpoint": "https://example.invalid",
            },
        ).as_dict()

        serialized = str(configuration)
        self.assertNotIn("redaction-fixture-one", serialized)
        self.assertNotIn("redaction-fixture-two", serialized)
        self.assertIn("REDACTED", serialized)
        self.assertEqual(configuration["lifecycle_state"], "CONFIGURED")

    def test_diagnostics_serialization_and_capability_detection(self):
        manager = ProviderManager(
            registry(),
            health={"status": "ok", "latest_measurement_time": "2026-06-29T10:00:00+08:00"},
            provider_observations={"latest": [{"provider_id": "solaredge"}]},
            promotion_summary={"latest_promotions": [{"provider_id": "solaredge"}]},
        )
        provider = manager.get("solaredge").as_dict()

        self.assertEqual(provider["diagnostics"]["observation_count"], 1)
        self.assertEqual(provider["diagnostics"]["promotion_count"], 1)
        self.assertTrue(provider["capability_summary"]["detected"])
        self.assertIn("daily_summary", provider["capability_summary"]["capabilities"])
        self.assertEqual(provider["onboarding"]["state"], "CONNECTED")
        self.assertTrue(provider["onboarding"]["connected"])
        self.assertTrue(provider["onboarding"]["healthy"])
        self.assertEqual(provider["onboarding"]["sync_frequency"], "Collector schedule")
        self.assertIn("why_required", provider["onboarding"])
        self.assertIn("recovery_steps", provider["onboarding"])

    def test_provider_management_exposes_standard_onboarding_states(self):
        payload = ProviderManager(registry()).as_dict()

        self.assertEqual(
            payload["onboarding_states"],
            [state.value for state in ProviderOnboardingState],
        )

    def test_missing_provider_has_plain_language_recovery_guidance(self):
        provider = ProviderManager(registry(status=ProviderStatus.MISSING)).get("solaredge").as_dict()

        self.assertEqual(provider["onboarding"]["state"], "NOT_CONFIGURED")
        self.assertFalse(provider["onboarding"]["connected"])
        self.assertIn("Cookie", provider["onboarding"]["required_information"])
        self.assertIn("Run Test Connection", provider["onboarding"]["recovery_steps"])

    def test_connection_testing_reports_success_and_warnings(self):
        connected = ProviderManager(registry()).test_connection("solaredge").as_dict()
        missing = ProviderManager(registry(status=ProviderStatus.MISSING)).test_connection("solaredge").as_dict()

        self.assertTrue(connected["success"])
        self.assertEqual(connected["authentication_status"], "authenticated")
        self.assertFalse(missing["success"])
        self.assertIn("Provider is not configured.", missing["warnings"])

    def test_provider_removal_and_reconnect_flow_actions_are_model_driven(self):
        manager = ProviderManager(registry())

        self.assertEqual(manager.transition("solaredge", "Reconnect").provider_id, "solaredge")
        self.assertEqual(manager.transition("solaredge", "Remove").provider_id, "solaredge")
        with self.assertRaises(ValueError):
            manager.transition("solaredge", "Rotate The Moon")

    def test_future_provider_is_available_for_configuration(self):
        manager = ProviderManager(registry())
        tesla = manager.get("tesla")

        self.assertIsNotNone(tesla)
        self.assertEqual(tesla.lifecycle_state, ProviderConnectionStatus.NOT_CONFIGURED)
        self.assertIn("Configure", tesla.actions)

    def test_api_serialization_never_returns_sensitive_values(self):
        manager = ProviderManager(registry())
        configuration = manager.configure("solaredge", {"password": "redaction-fixture-three", "cookie": "redaction-fixture-four"})
        payload = manager.as_dict()
        config_payload = configuration.as_dict()

        self.assertIn("providers", payload)
        self.assertNotIn("redaction-fixture-three", str(config_payload))
        self.assertNotIn("redaction-fixture-four", str(config_payload))


if __name__ == "__main__":
    unittest.main()
