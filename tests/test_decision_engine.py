import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from engine.decision_engine import (
    DecisionConfidence,
    DecisionEngine,
    DecisionPriority,
    DecisionType,
)
from engine.models import CapabilityStatus, HouseholdCapabilitySummary
from engine.notification_engine import NotificationEngine, NotificationType


LOCAL = ZoneInfo("Australia/Perth")


def state(**overrides):
    data = {
        "generated_at": "2026-06-27T12:00:00+08:00",
        "household_id": "default_household",
        "summary": {"current_status": "healthy"},
        "tariff": {
            "active_bucket": "super_off_peak",
            "active_label": "Super off peak",
            "active_rate": 0.086,
            "next_lower_cost_window": "Super off peak now",
            "lowest_cost_window": "Super off peak now",
        },
        "collector": {"healthy": True, "status": "ok"},
        "evidence": {
            "confidence": "HIGH",
            "quality": "HIGH",
            "data_sources_used": ["Household State"],
        },
    }
    data.update(overrides)
    return data


def context(**overrides):
    data = {
        "tariff": "LOWEST_COST",
        "charging": "CHEAP_WINDOW_ACTIVE",
        "solar": "SOLAR_AVAILABLE",
        "billing": "ON_TRACK",
        "confidence": "HIGH",
        "data_sources_used": ["Household Context"],
    }
    data.update(overrides)
    return data


def capabilities(*present):
    return HouseholdCapabilitySummary(
        household_id="default_household",
        generated_at="2026-06-27T12:00:00+08:00",
        capabilities=[
            CapabilityStatus(name, name, present=name in present, configured=name in present)
            for name in ("ev", "tariff", "grid_smart_meter", "solar")
        ],
    )


class DecisionEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = DecisionEngine()
        self.now = datetime(2026, 6, 27, 12, tzinfo=LOCAL)

    def build(self, **kwargs):
        defaults = {
            "household_state": state(),
            "context": context(),
            "capability_summary": capabilities("ev", "tariff", "grid_smart_meter", "solar"),
            "now": self.now,
        }
        defaults.update(kwargs)
        return self.engine.build(**defaults)

    def test_lowest_cost_window_generates_charge_now_decision(self):
        decisions = self.build()

        decision = decisions[0]
        self.assertEqual(decision.decision_type, DecisionType.CHARGE_NOW)
        self.assertEqual(decision.priority, DecisionPriority.HIGH)
        self.assertEqual(decision.confidence, DecisionConfidence.HIGH)
        self.assertEqual(decision.required_capabilities, ["ev", "tariff"])

    def test_insufficient_evidence_does_not_create_high_confidence_action(self):
        decisions = self.build(
            household_state=state(evidence={"confidence": "LOW", "quality": "LOW"}),
            context=context(confidence="LOW"),
        )

        types = [decision.decision_type for decision in decisions]
        self.assertIn(DecisionType.DATA_INSUFFICIENT, types)
        self.assertNotIn(DecisionType.CHARGE_NOW, types)
        self.assertTrue(all(decision.confidence != DecisionConfidence.HIGH for decision in decisions))

    def test_high_cost_tariff_waits_for_cheaper_window(self):
        decisions = self.build(
            household_state=state(tariff={"active_rate": 0.54, "next_lower_cost_window": "Off peak from 9:00 PM"}),
            context=context(tariff="HIGHEST_COST", charging="WAITING_FOR_CHEAPER_WINDOW"),
        )

        self.assertEqual(decisions[0].decision_type, DecisionType.WAIT_FOR_CHEAPER_WINDOW)

    def test_anomaly_drives_investigation_decision(self):
        decisions = self.build(
            anomalies=[
                {
                    "anomaly_id": "a1",
                    "category": "baseload",
                    "title": "Higher than normal overnight load",
                    "summary": "Overnight baseload is above normal.",
                    "severity": "warning",
                    "confidence": "MEDIUM",
                    "first_detected": "2026-06-27T06:00:00+08:00",
                    "data_sources_used": ["Anomaly Engine"],
                }
            ],
        )

        self.assertIn(DecisionType.INVESTIGATE_ANOMALY, [decision.decision_type for decision in decisions])
        anomaly_decision = next(decision for decision in decisions if decision.decision_type == DecisionType.INVESTIGATE_ANOMALY)
        self.assertEqual(anomaly_decision.related_anomaly_ids, ["a1"])

    def test_missing_capabilities_generate_review_setup(self):
        decisions = self.build(capability_summary=capabilities("tariff"))

        setup = next(decision for decision in decisions if decision.decision_type == DecisionType.REVIEW_SETUP)
        self.assertIn("grid_smart_meter", setup.missing_capabilities)
        self.assertEqual(setup.priority, DecisionPriority.HIGH)

    def test_serialization_and_summary(self):
        decisions = self.build()
        payload = decisions[0].as_dict()
        summary = self.engine.summary(decisions)

        self.assertIn("decision_id", payload)
        self.assertEqual(payload["type"], "CHARGE_NOW")
        self.assertIn("evidence", payload)
        self.assertEqual(summary["count"], len(decisions))
        self.assertIn("primary_decision", summary)

    def test_notification_engine_consumes_household_decisions(self):
        decisions = [decision.as_dict() for decision in self.build()]
        readiness = NotificationEngine().build(
            household_state_snapshot={"household_id": "default_household"},
            context=context(),
            household_decisions=decisions,
            evidence_quality="HIGH",
            now=self.now,
        )

        self.assertIn(NotificationType.CHARGE_NOW, [item.notification_type for item in readiness.candidates])


if __name__ == "__main__":
    unittest.main()
