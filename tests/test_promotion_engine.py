import unittest

from engine.promotion_engine import PromotionEngine


class PromotionEngineTests(unittest.TestCase):
    def test_identical_observation_is_duplicate(self):
        results = PromotionEngine().evaluate(
            [
                observation("a", value=4.2),
                observation("b", value=4.2),
            ],
            household_id="household",
        )

        self.assertEqual([item.decision.state.value for item in results], ["ACCEPTED", "DUPLICATE"])

    def test_newer_same_provider_observation_supersedes_older(self):
        results = PromotionEngine().evaluate(
            [
                observation("old", value=4.2, observed_at="2026-06-27T10:00:00", raw_reference="same"),
                observation("new", value=4.4, observed_at="2026-06-27T11:00:00", raw_reference="same"),
            ],
            household_id="household",
        )
        states = {item.candidate.observation_id: item.decision.state.value for item in results}

        self.assertEqual(states["old"], "SUPERSEDED")
        self.assertEqual(states["new"], "ACCEPTED")

    def test_negative_solar_generation_is_rejected(self):
        result = PromotionEngine().evaluate(
            [
                observation(
                    "bad",
                    observation_type="solar_generation",
                    capability="daily_summary",
                    value={"generation_kwh": -1.0},
                )
            ],
            household_id="household",
        )[0]

        self.assertEqual(result.decision.state.value, "REJECTED")
        self.assertEqual(result.decision.reasons[0].code, "impossible_negative")

    def test_provider_disagreement_outside_tolerance_is_conflict(self):
        results = PromotionEngine(tolerance_by_type={"grid_import": 0.2}).evaluate(
            [
                observation("a", provider_id="meter_a", observation_type="grid_import", capability="daily_summary", value=10.0),
                observation("b", provider_id="meter_b", observation_type="grid_import", capability="daily_summary", value=11.0),
            ],
            household_id="household",
        )

        self.assertEqual(results[1].decision.state.value, "CONFLICT")
        self.assertEqual(results[1].decision.conflict.conflicting_observation_id, "a")

    def test_household_scope_mismatch_is_rejected(self):
        result = PromotionEngine().evaluate(
            [observation("wrong", household_id="other")],
            household_id="household",
        )[0]

        self.assertEqual(result.decision.state.value, "REJECTED")
        self.assertEqual(result.decision.reasons[0].code, "household_scope")

    def test_stale_observation_is_pending(self):
        result = PromotionEngine().evaluate(
            [observation("stale", freshness={"state": "stale"})],
            household_id="household",
        )[0]

        self.assertEqual(result.decision.state.value, "PENDING")
        self.assertEqual(result.decision.reasons[0].code, "stale_observation")

    def test_live_observation_rejects_invalid_units(self):
        result = PromotionEngine().evaluate(
            [observation("live", observation_type="solar_live_power", capability="live_power", value=1200, unit="kWh")],
            household_id="household",
        )[0]

        self.assertEqual(result.decision.state.value, "REJECTED")
        self.assertEqual(result.decision.reasons[0].code, "invalid_unit")

    def test_battery_soc_must_be_possible(self):
        result = PromotionEngine().evaluate(
            [observation("soc", observation_type="battery_soc", capability="battery_state", value=130, unit="%")],
            household_id="household",
        )[0]

        self.assertEqual(result.decision.state.value, "REJECTED")
        self.assertEqual(result.decision.reasons[0].code, "invalid_battery_soc")

    def test_signed_grid_and_battery_power_are_allowed(self):
        results = PromotionEngine().evaluate(
            [
                observation("grid", observation_type="grid_live_power", capability="live_power", value=-500, unit="W"),
                observation("battery", observation_type="battery_power", capability="battery_state", value=-1200, unit="W", raw_reference="battery"),
            ],
            household_id="household",
        )

        self.assertEqual([item.decision.state.value for item in results], ["ACCEPTED", "ACCEPTED"])

    def test_stale_live_observation_is_rejected(self):
        result = PromotionEngine().evaluate(
            [observation("stale-live", observation_type="home_live_power", capability="live_power", value=700, unit="W", freshness={"state": "stale"})],
            household_id="household",
        )[0]

        self.assertEqual(result.decision.state.value, "REJECTED")
        self.assertEqual(result.decision.reasons[0].code, "stale_live_observation")


def observation(
    observation_id,
    *,
    household_id="household",
    provider_id="provider",
    observation_type="ev_session",
    capability="ev_sessions",
    observed_at="2026-06-27T10:00:00",
    value=None,
    quality="high",
    freshness=None,
    raw_reference=None,
    unit=None,
):
    return {
        "observation_id": observation_id,
        "household_id": household_id,
        "provider_id": provider_id,
        "provider_type": "ev_charger",
        "capability": capability,
        "observation_type": observation_type,
        "observed_at": observed_at,
        "value": {"energy_kwh": 7.5} if value is None else value,
        "unit": unit,
        "quality": quality,
        "freshness": freshness,
        "raw_reference": raw_reference,
    }


if __name__ == "__main__":
    unittest.main()
