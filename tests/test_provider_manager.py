import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

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


def registry_with_home_assistant(status=ProviderStatus.AVAILABLE):
    base = registry(status).providers
    return ProviderRegistry(
        base + [
            Provider(
                provider_id="home_assistant",
                name="Home Assistant",
                provider_type=ProviderType.HOME_AUTOMATION,
                capabilities=[
                    ProviderCapability.LIVE_POWER,
                    ProviderCapability.BATTERY_STATE,
                    ProviderCapability.EV_SESSIONS,
                ],
                status=ProviderStatus.CONFIGURED,
                note="Home Assistant provider.",
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


def paths_read(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


class ProviderManagerTests(unittest.TestCase):
    def test_configuration_lifecycle_and_state_transitions(self):
        manager = ProviderManager(registry())
        provider = manager.get("solaredge")

        self.assertEqual(provider.lifecycle_state, ProviderConnectionStatus.CONNECTED)
        self.assertIn("Reconnect", provider.actions)
        self.assertIn("Remove", provider.actions)

    def test_credential_updates_are_redacted(self):
        with TemporaryDirectory() as directory:
            manager = ProviderManager(registry(status=ProviderStatus.MISSING), **manager_paths(directory))
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

    def test_required_cookie_must_be_present_before_configuration_is_ready(self):
        with TemporaryDirectory() as directory:
            manager = ProviderManager(registry(status=ProviderStatus.MISSING), **manager_paths(directory))
            configuration = manager.configure("solaredge", {"username": "user@example.com"}).as_dict()

        self.assertEqual(configuration["lifecycle_state"], "NOT_CONFIGURED")
        self.assertIn("Cookie", configuration["notes"])

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
        self.assertEqual(provider["onboarding"]["sync_frequency"], "Refresh schedule")
        self.assertIn("why_required", provider["onboarding"])
        self.assertIn("recovery_steps", provider["onboarding"])

    def test_provider_diagnostics_include_live_observation_types(self):
        manager = ProviderManager(
            registry(),
            provider_observations={
                "providers": [
                    {
                        "provider_id": "solaredge",
                        "observation_types": ["solar_live_power", "grid_import"],
                    }
                ],
                "latest": [],
            },
        )

        provider = manager.get("solaredge").as_dict()

        self.assertEqual(provider["diagnostics"]["live_observation_types"], ["solar_live_power"])

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
        self.assertEqual(connected["status"], "success")
        self.assertIn("Connection test succeeded", connected["user_message"])

    def test_solaredge_adapter_test_result_is_normalized_with_guidance(self):
        manager = ProviderManager(
            registry(),
            connection_testers={
                "solaredge": lambda: {
                    "success": False,
                    "authentication_status": "not_authenticated",
                    "provider_version": "solaredge-monitoring-local",
                    "available_datasets": [],
                    "warnings": ["SolarEdge returned HTTP 403"],
                    "response_time_ms": 125,
                    "http_status": "403",
                    "measurements": 0,
                }
            },
        )

        result = manager.test_connection("solaredge").as_dict()

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failure")
        self.assertIn("SolarEdge returned HTTP 403", result["user_message"])
        self.assertIn("Refresh the provider session", result["next_step"])
        self.assertEqual(result["technical_details"]["http_status"], "403")

    def test_runtime_health_is_included_in_onboarding(self):
        manager = ProviderManager(
            registry(),
            health={"status": "ok", "latest_measurement_time": "2026-06-29T10:00:00+08:00"},
            runtime_status={
                "providers": [
                    {
                        "provider_id": "solaredge",
                        "health": {
                            "state": "BACKING_OFF",
                            "next_run_at": "2026-06-30T10:05:00+08:00",
                            "execution_duration_ms": 1530,
                            "consecutive_failures": 2,
                        },
                    }
                ]
            },
        )

        onboarding = manager.get("solaredge").as_dict()["onboarding"]

        self.assertEqual(onboarding["runtime_state"], "BACKING_OFF")
        self.assertEqual(onboarding["collector_duration"], "1.5s")
        self.assertEqual(onboarding["consecutive_failures"], 2)
        self.assertIn("Retry scheduled", onboarding["retry_countdown"])
        self.assertIn("runtime_success", onboarding["health_factors"])

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

    def test_ocular_import_provider_does_not_request_account_credentials(self):
        provider = ProviderManager(registry()).get("ocular_export").as_dict()

        self.assertEqual(provider["configuration"]["editable_fields"], [])
        self.assertEqual(provider["configuration"]["credentials"], [])

    def test_api_serialization_never_returns_sensitive_values(self):
        with TemporaryDirectory() as directory:
            manager = ProviderManager(registry(), **manager_paths(directory))
            configuration = manager.configure("solaredge", {"password": "redaction-fixture-three", "cookie": "redaction-fixture-four"})
            payload = manager.as_dict()
            config_payload = configuration.as_dict()

        self.assertIn("providers", payload)
        self.assertNotIn("redaction-fixture-three", str(config_payload))
        self.assertNotIn("redaction-fixture-four", str(config_payload))

    def test_home_assistant_entity_mapping_is_serialized_and_persisted(self):
        with TemporaryDirectory() as directory:
            paths = manager_paths(directory)
            path = paths["entity_mapping_store_path"]
            manager = ProviderManager(registry_with_home_assistant(), **paths)

            configuration = manager.configure(
                "home_assistant",
                {
                    "api_key": "redaction-fixture-five",
                    "endpoint": "http://homeassistant.local:8123",
                    "entity_mappings": {
                        "solar_live_power": "sensor.solar_power",
                        "battery_soc": "sensor.battery_soc",
                    },
                },
            ).as_dict()
            reloaded = ProviderManager(
                registry_with_home_assistant(),
                **paths,
                provider_observations={
                    "latest": [
                        {
                            "provider_id": "home_assistant",
                            "raw_reference": "sensor.solar_power",
                            "observed_at": "2026-06-30T08:00:00+08:00",
                        }
                    ]
                },
            ).get("home_assistant").as_dict()

            self.assertEqual(configuration["entity_mapping"]["selected_mappings"]["solar_live_power"], "sensor.solar_power")
            self.assertEqual(reloaded["configuration"]["entity_mapping"]["selected_mappings"]["battery_soc"], "sensor.battery_soc")
            self.assertEqual(reloaded["diagnostics"]["entity_mapping"]["validation"]["status"], "warning")
            diagnostics_by_entity = {
                item["selected_entity"]: item
                for item in reloaded["diagnostics"]["entity_mapping"]["diagnostics"]
            }
            self.assertEqual(
                diagnostics_by_entity["sensor.solar_power"]["last_successful_observation"],
                "2026-06-30T08:00:00+08:00",
            )
            self.assertTrue(path.exists())

    def test_provider_action_save_test_reset_disable_enable_and_remove(self):
        with TemporaryDirectory() as directory:
            manager = ProviderManager(
                registry_with_home_assistant(),
                connection_testers={"home_assistant": lambda: {"success": True, "authentication_status": "authenticated", "warnings": []}},
                **manager_paths(directory),
            )

            save = manager.action(
                "home_assistant",
                "save",
                {
                    "endpoint": "http://homeassistant.local:8123",
                    "api_key": "redaction-fixture-home-assistant-token",
                    "entity_mappings": {"solar_live_power": "sensor.solar_power"},
                },
            ).as_dict()
            test = manager.action("home_assistant", "test").as_dict()
            reset = manager.action("home_assistant", "reset").as_dict()
            disabled = manager.action("home_assistant", "disable").as_dict()
            enabled = manager.action("home_assistant", "enable").as_dict()
            removed = manager.action("home_assistant", "remove").as_dict()

            self.assertTrue(save["success"])
            self.assertEqual(test["status"], "success")
            self.assertTrue(reset["success"])
            self.assertEqual(disabled["provider"]["lifecycle_state"], "DISABLED")
            self.assertTrue(enabled["success"])
            self.assertEqual(removed["provider"]["lifecycle_state"], "NOT_CONFIGURED")
            self.assertNotIn("redaction-fixture-home-assistant-token", str(save))
            self.assertNotIn("home_assistant_token", paths_read(manager_paths(directory)["secrets_path"]))

    def test_invalid_provider_configuration_is_rejected(self):
        with TemporaryDirectory() as directory:
            manager = ProviderManager(registry_with_home_assistant(), **manager_paths(directory))

            with self.assertRaises(ValueError):
                manager.action("home_assistant", "save", {"endpoint": "homeassistant.local", "api_key": "short"})

    def test_home_assistant_connection_test_exposes_entity_mapping_diagnostics(self):
        manager = ProviderManager(
            registry_with_home_assistant(),
            connection_testers={
                "home_assistant": lambda: {
                    "success": True,
                    "authentication_status": "authenticated",
                    "provider_version": "home-assistant-2026.6.1",
                    "available_datasets": ["live_power"],
                    "warnings": ["Battery state of charge is mapped to a missing entity."],
                    "response_time_ms": 44,
                    "entity_mapping": {
                        "validation": {"status": "warning", "warnings": ["Battery state of charge is mapped to a missing entity."]},
                        "selected_mappings": {"battery_soc": "sensor.missing_battery"},
                    },
                }
            },
        )

        result = manager.test_connection("home_assistant").as_dict()

        self.assertEqual(result["status"], "warning")
        self.assertIn("entity_mapping", result["technical_details"])
        self.assertIn("Battery state of charge", result["user_message"])


if __name__ == "__main__":
    unittest.main()
