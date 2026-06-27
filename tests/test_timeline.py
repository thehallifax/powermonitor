import unittest
from dataclasses import dataclass
from datetime import date

from engine.models import Decision, DecisionExplanation, FinancialImpact
from presentation.timeline import build_decision_timeline


@dataclass
class Summary:
    start: date = date(2026, 6, 6)
    end: date = date(2026, 6, 27)


def decision(
    decision_id: str,
    title: str,
    category: str = "Tariffs",
    impact: float = 4.2,
) -> Decision:
    return Decision(
        id=decision_id,
        title=title,
        summary=f"{title} summary",
        finding=f"{title} finding.",
        evidence={"test_metric": 1.0},
        financial_impact=FinancialImpact(billing_period=impact),
        recommendation=f"{title} recommendation.",
        confidence="Medium",
        priority="Medium",
        expires_at="2026-06-30",
        data_sources_used=["SolarEdge interval data", "Tariff settings"],
        category=category,
        action_type="load_shift",
        explanation=DecisionExplanation(
            why_this_matters="Existing Decision Engine reasoning.",
            evidence_used=["Test metric: 1.00"],
            financial_impact="$4.20 this billing period",
            confidence_reason="Medium confidence from existing source data.",
            data_sources_used=["SolarEdge interval data", "Tariff settings"],
            freshness_completeness_note=None,
        ),
    )


class DecisionTimelineTests(unittest.TestCase):
    def test_builds_chronological_timeline_from_existing_outputs(self):
        comparison = {
            "period_a": {"start": "2026-06-06", "end": "2026-06-27"},
            "deltas": {"net_cost": -12.5},
        }

        timeline = build_decision_timeline(
            Summary(),
            [
                decision("peak_cost_reduction", "Reduce Peak-period imports"),
                decision("ev_session_low_cost_window", "EV charging is mostly cheap", "EV"),
            ],
            comparison,
        )

        self.assertEqual([entry.timestamp for entry in timeline.entries], sorted(entry.timestamp for entry in timeline.entries))
        self.assertEqual(timeline.entries[0].title, "New billing period")
        self.assertIn("Bill tracking improved", [entry.title for entry in timeline.entries])
        self.assertIn("High peak usage", [entry.title for entry in timeline.entries])
        self.assertIn("EV charged in cheapest tariff", [entry.title for entry in timeline.entries])

    def test_timeline_entries_include_required_presentation_fields(self):
        timeline = build_decision_timeline(
            Summary(),
            [decision("solar_self_consumption", "Use more solar at home", "Solar")],
        )
        payload = timeline.as_dict()
        decision_entry = next(
            entry for entry in payload["entries"]
            if entry["title"] == "Solar utilisation opportunity"
        )

        self.assertEqual(
            set(decision_entry),
            {
                "timestamp",
                "title",
                "summary",
                "financial_impact",
                "confidence",
                "evidence_sources",
            },
        )
        self.assertEqual(decision_entry["financial_impact"], "$4.20 this billing period")
        self.assertEqual(decision_entry["evidence_sources"], ["SolarEdge interval data", "Tariff settings"])


if __name__ == "__main__":
    unittest.main()
