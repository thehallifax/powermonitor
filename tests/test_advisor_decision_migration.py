import unittest
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_ROOT = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import collector.report as report
from engine.advisor_presenter import explain_household_decisions
from engine.briefing_engine import BriefingEngine
from engine.models import AdvisorCollection, Decision, EvidenceTrail, FinancialImpact
from engine.notification_engine import NotificationEngine, NotificationType
from presentation.timeline import build_decision_timeline


LOCAL = ZoneInfo("Australia/Perth")


def household_decision(decision_type="WAIT_FOR_CHEAPER_WINDOW", *, decision_id="hd1"):
    return {
        "decision_id": decision_id,
        "household_id": "default_household",
        "type": decision_type,
        "decision_type": decision_type,
        "priority": "high",
        "confidence": "HIGH",
        "status": "active",
        "created_at": "2026-06-27T18:00:00+08:00",
        "reasons": [{"code": "current_tariff_high", "summary": "Current tariff context is high-cost."}],
        "evidence": [{"source": "HouseholdContext", "signal": "tariff", "value": "HIGHEST_COST", "quality": "HIGH"}],
        "outcome": {"status": "wait", "expected_effect": "Wait for the next lower-cost tariff window."},
        "financial_impact": {"today": 1.25, "billing_period": None, "yearly": None},
        "data_sources_used": ["Household Context", "DecisionEngine"],
    }


def recommendation():
    return Decision(
        id="ev_charging_optimisation",
        title="EV charging reviewed",
        summary="Charging can be timed better.",
        finding="Charging can be timed better.",
        evidence={"priority_score": 80},
        financial_impact=FinancialImpact(today=1.25),
        recommendation="Wait for a cheaper window.",
        confidence="High",
        priority="High",
        expires_at="2026-06-27",
        data_sources_used=["Tariff settings"],
        category="EV",
        action_type="tariff_window",
    )


@dataclass
class Summary:
    start: date = date(2026, 6, 1)
    end: date = date(2026, 6, 27)
    intervals: int = 1


class AdvisorDecisionMigrationTests(unittest.TestCase):
    def test_advisor_presenter_serializes_household_decision_reference(self):
        advisors = explain_household_decisions([household_decision()])

        payload = advisors[0].as_dict()

        self.assertEqual(payload["decision_reference"]["decision_id"], "hd1")
        self.assertEqual(payload["decision"]["type"], "WAIT_FOR_CHEAPER_WINDOW")
        self.assertEqual(payload["advisor_type"], "EV_CHARGING")
        self.assertIn("Decision Engine", payload["confidence_reason"])

    def test_briefing_summarizes_household_decisions(self):
        decision = household_decision("CHARGE_NOW")
        advisors = AdvisorCollection(explain_household_decisions([decision]))

        briefing = BriefingEngine().build(
            advisors,
            date(2026, 6, 27),
            household_decisions=[decision],
        )

        payload = briefing.as_dict()
        self.assertEqual(payload["top_priorities"][0]["decision_reference"]["decision_id"], "hd1")
        self.assertEqual(payload["top_priorities"][0]["recommendation"], "Wait for the next lower-cost tariff window.")
        self.assertIn("DecisionEngine", payload["overall_freshness"])

    def test_recommendation_mapping_exposes_household_decision(self):
        data = report.recommendation_with_decision_dict(recommendation(), [household_decision()])
        markup = report.recommendation_centre_html([recommendation()], household_decisions=[household_decision()])

        self.assertEqual(data["household_decision_id"], "hd1")
        self.assertEqual(data["decision"]["type"], "WAIT_FOR_CHEAPER_WINDOW")
        self.assertIn("Household decision", markup)
        self.assertIn("Wait For Cheaper Window", markup)

    def test_notification_candidates_reference_household_decisions(self):
        readiness = NotificationEngine().build(
            household_state_snapshot={"household_id": "default_household"},
            household_decisions=[household_decision("CHARGE_NOW")],
            evidence_quality="HIGH",
            now=datetime(2026, 6, 27, 12, tzinfo=LOCAL),
        )

        candidate = readiness.candidates[0]
        self.assertEqual(candidate.notification_type, NotificationType.CHARGE_NOW)
        self.assertTrue(candidate.source_event_key.startswith("decision:"))
        self.assertEqual(candidate.evidence[0].source, "DecisionEngine")

    def test_timeline_entries_reference_household_decisions(self):
        timeline = build_decision_timeline(
            Summary(),
            [],
            include_billing_period=False,
            household_decisions=[household_decision()],
        )

        entry = timeline.entries[0].as_dict()
        self.assertEqual(entry["decision_id"], "hd1")
        self.assertEqual(entry["decision_type"], "WAIT_FOR_CHEAPER_WINDOW")
        self.assertEqual(entry["event_type"], "household_decision_wait_for_cheaper_window")


if __name__ == "__main__":
    unittest.main()
