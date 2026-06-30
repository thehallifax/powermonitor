import unittest

from collector.home_assistant_provider import (
    HomeAssistantAdapter,
    discover_capabilities,
    observations_from_entities,
)
from engine.provider_entity_mapping import build_home_assistant_entity_mapping_plan
from engine.provider_framework import ProviderCapability, ProviderObservationType
from engine.provider_runtime import ProviderRuntime


HOME_ASSISTANT_STATES = [
    {
        "entity_id": "sensor.solar_production_power",
        "state": "4200",
        "last_updated": "2026-06-30T08:00:00+08:00",
        "attributes": {"unit_of_measurement": "W", "device_class": "power", "friendly_name": "Solar production power"},
    },
    {
        "entity_id": "sensor.house_energy_total",
        "state": "123.4",
        "last_updated": "2026-06-30T08:01:00+08:00",
        "attributes": {"unit_of_measurement": "kWh", "device_class": "energy", "friendly_name": "House energy total"},
    },
    {
        "entity_id": "sensor.battery_state_of_charge",
        "state": "82",
        "last_updated": "2026-06-30T08:02:00+08:00",
        "attributes": {"unit_of_measurement": "%", "device_class": "battery", "friendly_name": "Battery state of charge"},
    },
    {
        "entity_id": "sensor.ev_charger_power",
        "state": "7.2",
        "last_updated": "2026-06-30T08:03:00+08:00",
        "attributes": {"unit_of_measurement": "kW", "device_class": "power", "friendly_name": "EV charger power"},
    },
    {
        "entity_id": "binary_sensor.ev_connected",
        "state": "on",
        "last_updated": "2026-06-30T08:04:00+08:00",
        "attributes": {"friendly_name": "EV connected"},
    },
]


class HomeAssistantProviderTests(unittest.TestCase):
    def test_discovers_power_energy_solar_battery_and_ev_capabilities(self):
        capabilities = discover_capabilities(HOME_ASSISTANT_STATES)

        self.assertIn(ProviderCapability.DAILY_SUMMARY, capabilities)
        self.assertIn(ProviderCapability.INTERVAL_HISTORY, capabilities)
        self.assertIn(ProviderCapability.BATTERY_STATE, capabilities)
        self.assertIn(ProviderCapability.EV_SESSIONS, capabilities)

    def test_maps_entities_to_provider_observations(self):
        observations = observations_from_entities(HOME_ASSISTANT_STATES)

        self.assertTrue(all(item.provider_id == "home_assistant" for item in observations))
        self.assertIn(ProviderObservationType.SOLAR_LIVE_POWER, {item.observation_type for item in observations})
        self.assertIn(ProviderObservationType.BATTERY_SOC, {item.observation_type for item in observations})
        self.assertIn(ProviderObservationType.EV_CHARGER_LIVE_POWER, {item.observation_type for item in observations})
        self.assertIn(ProviderObservationType.EV_CONNECTED_STATE, {item.observation_type for item in observations})
        self.assertTrue(all(item.raw_reference for item in observations))

    def test_groups_entities_and_recommends_mapping_targets(self):
        plan = build_home_assistant_entity_mapping_plan(HOME_ASSISTANT_STATES).as_dict()

        self.assertTrue(plan["categories"]["solar"])
        self.assertTrue(plan["categories"]["battery"])
        self.assertTrue(plan["categories"]["ev_charger"])
        self.assertEqual(plan["recommended_mappings"]["solar_live_power"], "sensor.solar_production_power")
        self.assertEqual(plan["recommended_mappings"]["battery_soc"], "sensor.battery_state_of_charge")
        self.assertEqual(plan["validation"]["status"], "ok")

    def test_manual_entity_mapping_overrides_auto_discovery(self):
        observations = observations_from_entities(
            HOME_ASSISTANT_STATES,
            {"home_live_power": "sensor.solar_production_power"},
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observation_type, ProviderObservationType.HOME_LIVE_POWER)
        self.assertEqual(observations[0].raw_reference, "sensor.solar_production_power")

    def test_invalid_manual_mapping_warns_and_is_not_collected(self):
        plan = build_home_assistant_entity_mapping_plan(
            HOME_ASSISTANT_STATES,
            {"battery_soc": "sensor.ev_charger_power"},
        ).as_dict()
        observations = observations_from_entities(
            HOME_ASSISTANT_STATES,
            {"battery_soc": "sensor.ev_charger_power"},
        )

        self.assertEqual(plan["validation"]["status"], "warning")
        self.assertIn("invalid_unit", plan["validation"]["target_status"].values())
        self.assertEqual(observations, [])

    def test_mapping_diagnostics_mark_stale_selected_entities(self):
        stale_states = [
            {
                **HOME_ASSISTANT_STATES[0],
                "last_updated": "2020-01-01T00:00:00+00:00",
            }
        ]

        plan = build_home_assistant_entity_mapping_plan(
            stale_states,
            {"solar_live_power": "sensor.solar_production_power"},
        ).as_dict()

        self.assertEqual(plan["validation"]["status"], "ok")
        self.assertEqual(plan["diagnostics"][0]["health"], "stale")

    def test_connection_test_validates_authentication_and_version(self):
        def request_json(_base_url, _token, path):
            if path == "api/config":
                return {"version": "2026.6.1"}
            if path == "api/states":
                return HOME_ASSISTANT_STATES
            raise AssertionError(path)

        adapter = HomeAssistantAdapter(
            base_url="http://homeassistant.local:8123",
            token="fixture",
            request_json=request_json,
        )

        result = adapter.test_connection()

        self.assertTrue(result["success"])
        self.assertEqual(result["authentication_status"], "authenticated")
        self.assertEqual(result["provider_version"], "home-assistant-2026.6.1")
        self.assertIn("daily_summary", result["available_datasets"])
        self.assertEqual(result["measurements"], len(HOME_ASSISTANT_STATES))
        self.assertIn("entity_mapping", result)
        self.assertIn("recommended_mappings", result["entity_mapping"])

    def test_missing_token_returns_plain_failure(self):
        adapter = HomeAssistantAdapter(base_url="http://homeassistant.local:8123", token="")

        result = adapter.test_connection()

        self.assertFalse(result["success"])
        self.assertEqual(result["authentication_status"], "not_authenticated")
        self.assertIn("long-lived access token", result["warnings"][0])

    def test_runtime_persists_home_assistant_observations(self):
        class FakeDataEngine:
            def __init__(self):
                self.persisted = []

            def persist_provider_observations(self, observations):
                self.persisted.extend(observations)
                return [{"provider_id": item.provider_id} for item in observations]

        def request_json(_base_url, _token, path):
            if path == "api/states":
                return HOME_ASSISTANT_STATES
            if path == "api/config":
                return {"version": "2026.6.1"}
            raise AssertionError(path)

        engine = FakeDataEngine()
        adapter = HomeAssistantAdapter(base_url="http://homeassistant.local:8123", token="fixture", request_json=request_json)
        runtime = ProviderRuntime([adapter], data_engine=engine)

        result = runtime.run_provider("home_assistant")

        self.assertTrue(result.success)
        self.assertGreaterEqual(result.metrics.observations_submitted, 4)
        self.assertTrue(all(item.provider_id == "home_assistant" for item in engine.persisted))


if __name__ == "__main__":
    unittest.main()
