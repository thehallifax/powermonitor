import unittest

from engine.anomaly_engine import AnomalyCategory, AnomalyConfidence, AnomalyEngine


def behaviour(confidence="HIGH"):
    return {
        "household_id": "default_household",
        "generated_at": "2026-06-29T10:00:00+08:00",
        "confidence": confidence,
        "baseload": {
            "overnight_baseload_watts": 300.0,
            "normal_variance_watts": 20.0,
        },
        "daily_usage": {},
        "ev": {
            "preferred_charging_window": "23:00-00:00",
            "confidence": "HIGH",
        },
        "solar": {
            "sunny_day_export_pct": 35.0,
            "self_consumption_tendency": "high",
            "confidence": "HIGH",
        },
    }


def household_state(**overrides):
    data = {
        "household_id": "default_household",
        "generated_at": "2026-06-29T12:00:00+08:00",
        "energy": {
            "solar_generated_kwh_today": 8.0,
            "solar_used_kwh_today": 5.5,
            "solar_exported_kwh_today": 2.5,
        },
        "power": {"import_export_state": "exporting"},
        "evidence": {"confidence": "HIGH"},
    }
    data.update(overrides)
    return data


def context(**overrides):
    data = {
        "solar": "SOLAR_SURPLUS_POSSIBLE",
        "charging": "CHEAP_WINDOW_ACTIVE",
    }
    data.update(overrides)
    return data


class AnomalyEngineTests(unittest.TestCase):
    def detect(self, **kwargs):
        defaults = {
            "household_state": household_state(),
            "context": context(),
            "state_changes": [],
            "behaviour": behaviour(),
            "current_observations": {
                "overnight_baseload_watts": 310.0,
                "current_daily_cost": 4.0,
                "normal_daily_cost": 4.2,
            },
        }
        defaults.update(kwargs)
        return AnomalyEngine().detect(**defaults)

    def test_normal_household_behaviour_produces_no_anomalies(self):
        self.assertEqual(self.detect(), [])

    def test_detects_overnight_load_anomalies(self):
        anomalies = self.detect(current_observations={"overnight_baseload_watts": 430.0})

        self.assertEqual(anomalies[0].category, AnomalyCategory.BASELOAD)
        self.assertEqual(anomalies[0].title, "Higher than normal overnight load")
        self.assertEqual(anomalies[0].confidence, AnomalyConfidence.HIGH)

    def test_detects_ev_charging_anomalies(self):
        anomalies = self.detect(
            current_observations={
                "overnight_baseload_watts": 300.0,
                "current_ev_charging_window": "14:00-15:00",
                "current_ev_session_kwh": 25.0,
                "normal_ev_session_kwh": 12.0,
            }
        )

        titles = {item.title for item in anomalies}
        self.assertIn("EV charging outside normal hours", titles)
        self.assertIn("Unusually large EV charging session", titles)

    def test_detects_solar_anomalies(self):
        anomalies = self.detect(
            household_state=household_state(
                energy={
                    "solar_generated_kwh_today": 10.0,
                    "solar_used_kwh_today": 2.0,
                    "solar_exported_kwh_today": 8.0,
                },
                power={"import_export_state": "importing"},
            )
        )

        titles = {item.title for item in anomalies}
        self.assertIn("Unusually high solar exports", titles)
        self.assertIn("Reduced solar self-consumption", titles)
        self.assertIn("Unexpected daytime imports", titles)

    def test_detects_billing_anomalies(self):
        anomalies = self.detect(
            current_observations={
                "overnight_baseload_watts": 300.0,
                "current_daily_cost": 8.0,
                "normal_daily_cost": 4.0,
            }
        )

        self.assertEqual(anomalies[0].category, AnomalyCategory.BILLING)
        self.assertEqual(anomalies[0].title, "Higher than expected daily cost")

    def test_detects_collector_anomalies_and_serializes(self):
        anomalies = self.detect(
            household_state=household_state(evidence={"confidence": "LOW"}),
            state_changes=[
                {
                    "change_type": "provider_degraded",
                    "title": "Provider degraded",
                    "summary": "Provider data is no longer trusted.",
                    "detected_at": "2026-06-29T12:00:00+08:00",
                }
            ],
        )
        payload = [item.as_dict() for item in anomalies]
        titles = {item["title"] for item in payload}

        self.assertIn("Unusual evidence degradation", titles)
        self.assertIn("Unexpected provider behaviour", titles)
        self.assertIn("evidence", payload[0])
        self.assertIn("first_detected", payload[0])

    def test_low_behaviour_confidence_degrades_anomaly_confidence(self):
        anomalies = self.detect(
            behaviour=behaviour(confidence="LOW"),
            current_observations={"overnight_baseload_watts": 430.0},
        )

        self.assertEqual(anomalies[0].confidence, AnomalyConfidence.LOW)


if __name__ == "__main__":
    unittest.main()
