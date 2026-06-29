import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from engine.evidence_quality import EvidenceQuality
from engine.models import Advisor, AdvisorCollection, EvidenceTrail, FinancialImpact
from engine.notification_engine import (
    NotificationEngine,
    NotificationPreferences,
    NotificationPriority,
    NotificationQuietHours,
    NotificationRulePreference,
    NotificationSuppressionReason,
    NotificationType,
)


LOCAL = ZoneInfo("Australia/Perth")


def state_change(change_type, *, household_id="default_household", event_key=None):
    return {
        "change_id": event_key or f"{household_id}|{change_type}|snapshot-2",
        "event_key": event_key or f"{household_id}|{change_type}|snapshot-2",
        "household_id": household_id,
        "change_type": change_type,
        "severity": "opportunity",
        "title": change_type.replace("_", " ").title(),
        "summary": f"{change_type} was detected.",
        "detected_at": "2026-06-27T12:00:00+08:00",
        "evidence": [],
    }


def setup(completion_pct=100, household_id="default_household"):
    return {
        "household_id": household_id,
        "progress": {
            "completion_pct": completion_pct,
            "remaining_tasks": [] if completion_pct >= 100 else [{"title": "Connect solar provider"}],
        },
    }


def context(**overrides):
    data = {
        "tariff": "HIGHEST_COST",
        "solar": "SOLAR_AVAILABLE",
        "confidence": "HIGH",
    }
    data.update(overrides)
    return data


def advisor(advisor_type, status, *, advisor_id=None, next_review_time=None):
    return Advisor(
        advisor_id=advisor_id or advisor_type.lower(),
        advisor_type=advisor_type,
        status=status,
        headline=f"{advisor_type} {status}",
        summary="Advisor summary.",
        recommendation="Recommended action.",
        estimated_financial_impact=FinancialImpact(today=1.0),
        confidence="High",
        confidence_reason="Test confidence.",
        evidence_trail=EvidenceTrail(),
        freshness="fresh",
        supporting_data_sources=["test"],
        next_review_time=next_review_time,
    )


class NotificationEngineTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 27, 12, tzinfo=LOCAL)
        self.engine = NotificationEngine()

    def build(self, **kwargs):
        defaults = {
            "state_changes": [],
            "household_state_snapshot": {"household_id": "default_household"},
            "context": context(),
            "advisors": AdvisorCollection([advisor("EV_CHARGING", "WAIT")]),
            "setup": setup(),
            "evidence_quality": EvidenceQuality.HIGH,
            "now": self.now,
        }
        defaults.update(kwargs)
        return self.engine.build(**defaults)

    def test_generates_low_cost_candidate_when_ev_capability_exists(self):
        readiness = self.build(
            state_changes=[state_change("entered_low_cost_window")],
            advisors=AdvisorCollection([advisor("EV_CHARGING", "WAIT")]),
        )

        candidate = [
            item for item in readiness.ready_candidates
            if item.notification_type == NotificationType.LOW_COST_WINDOW_STARTED
        ][0]
        self.assertEqual(candidate.notification_type, NotificationType.LOW_COST_WINDOW_STARTED)
        self.assertTrue(candidate.ready)
        self.assertEqual(candidate.household_id, "default_household")

    def test_suppresses_low_evidence_non_health_candidates(self):
        readiness = self.build(
            state_changes=[state_change("entered_low_cost_window")],
            evidence_quality=EvidenceQuality.LOW,
        )

        candidate = readiness.candidates[0]
        self.assertFalse(candidate.ready)
        self.assertEqual(candidate.suppression_reason, NotificationSuppressionReason.LOW_EVIDENCE_QUALITY)

    def test_health_warning_survives_low_evidence_quality(self):
        readiness = self.build(
            state_changes=[state_change("collector_went_stale")],
            evidence_quality=EvidenceQuality.LOW,
        )

        candidate = readiness.ready_candidates[0]
        self.assertEqual(candidate.notification_type, NotificationType.COLLECTOR_STALE)

    def test_suppresses_context_irrelevant_solar_opportunity_at_night(self):
        readiness = self.build(
            state_changes=[],
            advisors=AdvisorCollection([advisor("SOLAR", "OPPORTUNITY")]),
            context=context(solar="SOLAR_IMPOSSIBLE"),
            allow_legacy_advisor_notifications=True,
        )

        candidate = readiness.candidates[0]
        self.assertEqual(candidate.notification_type, NotificationType.SOLAR_OPPORTUNITY)
        self.assertFalse(candidate.ready)
        self.assertEqual(candidate.suppression_reason, NotificationSuppressionReason.CONTEXT_IRRELEVANT)

    def test_duplicate_event_is_suppressed(self):
        duplicate = state_change("entered_peak", event_key="same-event")
        readiness = self.build(state_changes=[duplicate, duplicate])

        peak_candidates = [
            candidate for candidate in readiness.candidates
            if candidate.notification_type == NotificationType.PEAK_PERIOD_STARTED
        ]
        ready = [candidate for candidate in peak_candidates if candidate.ready]
        suppressed = [candidate for candidate in peak_candidates if not candidate.ready]

        self.assertEqual(len(ready), 1)
        self.assertEqual(suppressed[0].suppression_reason, NotificationSuppressionReason.RECENT_DUPLICATE)

    def test_expired_advisor_candidate_is_suppressed(self):
        expired = (self.now - timedelta(minutes=5)).isoformat(timespec="seconds")
        readiness = self.build(
            state_changes=[],
            advisors=AdvisorCollection([advisor("EV_CHARGING", "CHARGE_NOW", next_review_time=expired)]),
            allow_legacy_advisor_notifications=True,
        )

        candidate = readiness.candidates[0]
        self.assertEqual(candidate.notification_type, NotificationType.CHARGE_NOW)
        self.assertFalse(candidate.ready)
        self.assertEqual(candidate.suppression_reason, NotificationSuppressionReason.EXPIRED)

    def test_setup_incomplete_generates_suppressed_advice_when_unreliable(self):
        readiness = self.build(
            state_changes=[state_change("entered_low_cost_window")],
            setup=setup(completion_pct=30),
        )

        low_cost = [item for item in readiness.candidates if item.notification_type == NotificationType.LOW_COST_WINDOW_STARTED][0]
        setup_candidate = [item for item in readiness.candidates if item.notification_type == NotificationType.SETUP_INCOMPLETE][0]
        self.assertEqual(low_cost.suppression_reason, NotificationSuppressionReason.SETUP_INCOMPLETE)
        self.assertTrue(setup_candidate.ready)

    def test_household_id_is_preserved_in_candidates_and_serialization(self):
        readiness = self.build(
            state_changes=[state_change("collector_recovered", household_id="household_b")],
            household_state_snapshot={"household_id": "household_b"},
            setup=setup(household_id="household_b"),
        )
        data = readiness.as_dict()

        self.assertEqual(readiness.household_id, "household_b")
        self.assertEqual(data["household_id"], "household_b")
        self.assertEqual(data["candidates"][0]["household_id"], "household_b")
        self.assertIn("ready_count", data)
        self.assertIn("suppression_counts", data)

    def test_preferences_suppress_disabled_notification_type(self):
        readiness = self.build(
            state_changes=[state_change("entered_low_cost_window")],
            preferences=NotificationPreferences(
                rule_preferences=[
                    NotificationRulePreference(NotificationType.LOW_COST_WINDOW_STARTED, enabled=False)
                ]
            ),
        )

        candidate = [
            item for item in readiness.candidates
            if item.notification_type == NotificationType.LOW_COST_WINDOW_STARTED
        ][0]
        self.assertEqual(candidate.suppression_reason, NotificationSuppressionReason.TYPE_DISABLED)

    def test_preferences_suppress_below_priority_threshold(self):
        readiness = self.build(
            state_changes=[state_change("entered_low_cost_window")],
            preferences=NotificationPreferences(priority_threshold=NotificationPriority.HIGH),
        )

        candidate = [
            item for item in readiness.candidates
            if item.notification_type == NotificationType.LOW_COST_WINDOW_STARTED
        ][0]
        self.assertEqual(candidate.suppression_reason, NotificationSuppressionReason.BELOW_PRIORITY_THRESHOLD)

    def test_quiet_hours_suppress_non_critical_candidates(self):
        readiness = self.build(
            state_changes=[state_change("entered_peak")],
            preferences=NotificationPreferences(
                quiet_hours=NotificationQuietHours(enabled=True, start="11:00", end="13:00")
            ),
        )

        candidate = [
            item for item in readiness.candidates
            if item.notification_type == NotificationType.PEAK_PERIOD_STARTED
        ][0]
        self.assertEqual(candidate.suppression_reason, NotificationSuppressionReason.QUIET_HOURS)

    def test_recent_sent_history_suppresses_duplicate_within_cooldown(self):
        readiness = self.build(
            state_changes=[state_change("entered_peak", event_key="peak-now")],
            history=[
                {
                    "stable_key": "peak-now",
                    "notification_type": "peak_period_started",
                    "delivery_state": "sent",
                    "sent_at": "2026-06-27T11:30:00+08:00",
                }
            ],
            preferences=NotificationPreferences(cooldown_minutes=60),
        )

        candidate = [
            item for item in readiness.candidates
            if item.notification_type == NotificationType.PEAK_PERIOD_STARTED
        ][0]
        self.assertEqual(candidate.suppression_reason, NotificationSuppressionReason.RECENT_DUPLICATE)


if __name__ == "__main__":
    unittest.main()
