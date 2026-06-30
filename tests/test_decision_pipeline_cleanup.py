import unittest
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from engine.briefing_engine import BriefingEngine
from engine.charging_advisor import ChargingAdvisor
from engine.decision_engine import (
    DecisionConfidence,
    DecisionEvidence,
    DecisionOutcome,
    DecisionPriority,
    DecisionReason,
    DecisionStatus,
    DecisionType,
    HouseholdDecision,
)
from engine.decision_pipeline import DecisionPipeline
from engine.models import Advisor, AdvisorCollection, EnergyScore, EvidenceTrail, FinancialImpact
from engine.notification_engine import NotificationEngine, NotificationType
from presentation.glance import build_glance_state


LOCAL = ZoneInfo("Australia/Perth")


@dataclass
class Summary:
    intervals: int = 10
    solar_generated_kwh: float = 3.0
    solar_exported_kwh: float = 0.0
    solar_export_pct: float = 0.0
    net_cost: float = 41.0
    ev_charging_kwh: float = 8.0
    ev_charging_source: str = "ocular_export"


@dataclass
class Settings:
    import_rates: dict


SETTINGS = Settings(
    import_rates={
        "super_off_peak": 0.086151,
        "overnight": 0.193841,
        "off_peak": 0.236916,
        "peak": 0.538446,
    }
)


def household_decision(
    decision_type,
    *,
    decision_id,
    priority=DecisionPriority.HIGH,
    confidence=DecisionConfidence.HIGH,
    effect="Use the platform-owned household decision.",
):
    return HouseholdDecision(
        decision_id=decision_id,
        household_id="default_household",
        decision_type=decision_type,
        priority=priority,
        confidence=confidence,
        status=DecisionStatus.ACTIVE,
        created_at="2026-06-29T12:00:00+08:00",
        reasons=[DecisionReason("test_reason", f"{decision_type.value} is supported by canonical evidence.")],
        evidence=[
            DecisionEvidence(
                source="DecisionEngine",
                signal="decision_type",
                value=decision_type.value,
                quality=confidence,
                observed_at="2026-06-29T12:00:00+08:00",
            )
        ],
        outcome=DecisionOutcome("active", effect),
        financial_impact={"today": 1.8, "billing_period": 18.0, "yearly": 216.0},
        data_sources_used=["Household State", "DecisionEngine"],
        related_state_change_ids=["state_change_1"],
    )


def advisor():
    return Advisor(
        advisor_id="decision_wait",
        advisor_type="EV_CHARGING",
        status="WAIT",
        headline="Advisor explanation",
        summary="Explains the household decision.",
        recommendation="Advisor compatibility text should not own the user action.",
        estimated_financial_impact=FinancialImpact(today=1.8),
        confidence="High",
        confidence_reason="Decision-backed explanation.",
        evidence_trail=EvidenceTrail(),
        freshness="DecisionEngine",
        supporting_data_sources=["DecisionEngine"],
        decision_id="wait_1",
        decision_type="WAIT_FOR_CHEAPER_WINDOW",
    )


def energy_score():
    return EnergyScore(
        score=88,
        label="Good",
        summary="Good household performance.",
        top_positive_factors=[],
        top_negative_factors=[],
        potential_score=92,
        confidence="High",
        data_sources_used=["DecisionEngine"],
        explanation=None,
    )


class DecisionPipelineCleanupTests(unittest.TestCase):
    def test_end_to_end_pipeline_links_decision_context_behaviour_and_evidence(self):
        output = DecisionPipeline().present(
            [household_decision(DecisionType.WAIT_FOR_CHEAPER_WINDOW, decision_id="wait_1")],
            advisor_explanations=[advisor()],
            household_state={"household_id": "default_household", "state": "ready"},
            context={"tariff": "HIGHEST_COST"},
            behaviour={"ev_charging": "overnight"},
        )

        presentation = output.recommendations[0]

        self.assertEqual(presentation.decision_id, "wait_1")
        self.assertEqual(presentation.decision["decision_id"], "wait_1")
        self.assertEqual(presentation.advisor_explanation["advisor_id"], "decision_wait")
        self.assertEqual(presentation.household_state["household_id"], "default_household")
        self.assertEqual(presentation.context["tariff"], "HIGHEST_COST")
        self.assertEqual(presentation.behaviour["ev_charging"], "overnight")
        self.assertEqual(presentation.state_change_ids, ["state_change_1"])
        self.assertEqual(presentation.evidence[0].source, "DecisionEngine")

    def test_briefing_consumes_recommendation_presentations(self):
        output = DecisionPipeline().present(
            [household_decision(DecisionType.WAIT_FOR_CHEAPER_WINDOW, decision_id="wait_1")],
            advisor_explanations=[advisor()],
        )

        briefing = BriefingEngine().build(
            AdvisorCollection([advisor()]),
            recommendation_presentations=output.recommendations,
        )

        self.assertEqual(briefing.top_priorities[0].decision_id, "wait_1")
        self.assertEqual(briefing.top_priorities[0].recommendation, "Use the platform-owned household decision.")

    def test_notification_generation_consumes_recommendation_presentations(self):
        output = DecisionPipeline().present(
            [household_decision(DecisionType.CHARGE_NOW, decision_id="charge_1")],
            advisor_explanations=[advisor()],
        )

        readiness = NotificationEngine().build(
            household_state_snapshot={"household_id": "default_household"},
            recommendation_presentations=output.recommendations,
            advisors=AdvisorCollection([advisor()]),
            allow_legacy_advisor_notifications=False,
            evidence_quality="HIGH",
            now=datetime(2026, 6, 29, 12, tzinfo=LOCAL),
        )

        self.assertEqual(readiness.candidates[0].notification_type, NotificationType.CHARGE_NOW)
        self.assertEqual(readiness.candidates[0].source_event_key, "decision:charge_1")
        self.assertEqual(readiness.candidates[0].evidence[0].source, "Recommendation")

    def test_legacy_advisor_notifications_can_be_disabled(self):
        readiness = NotificationEngine().build(
            household_state_snapshot={"household_id": "default_household"},
            advisors=AdvisorCollection([advisor()]),
            allow_legacy_advisor_notifications=False,
            evidence_quality="HIGH",
            now=datetime(2026, 6, 29, 12, tzinfo=LOCAL),
        )

        self.assertEqual(readiness.candidates, [])

    def test_glance_recommendation_text_comes_from_presentation(self):
        now = datetime(2026, 6, 29, 18, tzinfo=LOCAL)
        charging = ChargingAdvisor().advise(
            today_summary=Summary(),
            billing_summary=Summary(),
            settings=SETTINGS,
            health={"status": "ok", "report_age_minutes": 5},
            recommendations=[],
            projected_bill=82.0,
            now=now,
        )
        output = DecisionPipeline().present(
            [
                household_decision(
                    DecisionType.WAIT_FOR_CHEAPER_WINDOW,
                    decision_id="wait_1",
                    effect="Wait for the next lower-cost tariff window.",
                )
            ]
        )

        glance = build_glance_state(
            billing_summary=Summary(),
            charging_advice=charging,
            energy_score=energy_score(),
            projected_bill=82.0,
            bill_projection_basis="selected period",
            now=now,
            recommendation_presentation=output.recommendations[0],
        )

        self.assertEqual(glance.current_recommendation, "Wait for the next lower-cost tariff window.")
        self.assertIn("WAIT_FOR_CHEAPER_WINDOW", glance.reason)

    def test_recommendation_ordering_comes_from_adapter_priority(self):
        output = DecisionPipeline().present(
            [
                household_decision(DecisionType.NO_ACTION, decision_id="low", priority=DecisionPriority.LOW),
                household_decision(DecisionType.INVESTIGATE_ANOMALY, decision_id="critical", priority=DecisionPriority.CRITICAL),
            ]
        )

        self.assertEqual([item.decision_id for item in output.recommendations], ["critical", "low"])


if __name__ == "__main__":
    unittest.main()
