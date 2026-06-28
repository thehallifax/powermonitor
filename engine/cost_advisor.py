#!/usr/bin/env python3
"""Financial tracking advisor for PowerMonitor."""

from datetime import datetime
from typing import Any, Optional

from .evidence_quality import AdvisorEvidenceSummary, EvidenceQuality, EvidenceRequirement, EvidenceSource, cap_confidence_for_evidence
from .financial_engine import FinancialEngine
from .models import Advisor, Decision, DecisionTimeline, EvidenceItem, EvidenceTrail, FinancialImpact, HouseholdContext


class CostAdvisor:
    """Answers whether the current billing period is financially on track."""

    name = "cost_advisor"

    def __init__(
        self,
        financial_engine: Optional[FinancialEngine] = None,
        household_context: Optional[HouseholdContext] = None,
    ) -> None:
        self.household_context = household_context
        self.financial_engine = financial_engine or FinancialEngine()

    def advise(
        self,
        *,
        current_summary: Any,
        previous_summary: Optional[Any],
        settings: Any,
        recommendations: list[Decision],
        timeline: Optional[DecisionTimeline],
        health: dict[str, Any],
        comparison: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Advisor:
        observed_at = now or datetime.now().astimezone()
        projected, projected_end, projection_basis, projection_impact = self.financial_engine.project_bill(
            current_summary,
            settings,
        )
        freshness = self._freshness(current_summary, health)
        confidence = self._confidence(current_summary, previous_summary, health)
        evidence_summary = self._evidence_summary(
            current_summary=current_summary,
            previous_summary=previous_summary,
            health=health,
            projection_basis=projection_basis,
            confidence=confidence,
        )
        confidence = evidence_summary.confidence

        if confidence == "Low" and previous_summary is None:
            return self._unknown(projected, projected_end, projection_basis, freshness, observed_at, evidence_summary)

        previous_cost = self._number(previous_summary, "net_cost") if previous_summary is not None else None
        if previous_cost is None or previous_cost <= 0:
            return self._unknown(projected, projected_end, projection_basis, freshness, observed_at)

        difference = projected - previous_cost
        pct_difference = difference / previous_cost * 100
        status = self._status(difference, pct_difference, recommendations)
        headline = self._headline(status, difference, pct_difference)
        summary = self._summary(status, projected, previous_cost, difference, pct_difference, projected_end)
        recommendation = self._recommendation(status)
        confidence_reason = self._confidence_reason(confidence, current_summary, freshness, projection_basis)
        impact = FinancialImpact(
            billing_period=round(difference, 2),
            basis="Financial Engine projected bill minus previous billing period net cost",
        )
        evidence_trail = self._evidence_trail(
            projected=projected,
            previous_cost=previous_cost,
            difference=difference,
            pct_difference=pct_difference,
            projection_basis=projection_basis,
            freshness=freshness,
            confidence=confidence,
            current_summary=current_summary,
            comparison=comparison or {},
            recommendations=recommendations,
            timeline=timeline,
        )

        return Advisor(
            advisor_id="cost_advisor",
            advisor_type="COST",
            status=status,
            headline=headline,
            summary=summary,
            recommendation=recommendation,
            estimated_financial_impact=impact,
            confidence=confidence,
            confidence_reason=confidence_reason,
            evidence_trail=evidence_trail,
            freshness=freshness,
            supporting_data_sources=[
                "Financial Engine",
                "Current billing period",
                "Previous billing period",
                "Recommendation Engine",
                "Decision timeline",
                "Configured tariff rates",
            ],
            next_review_time=projected_end.isoformat(),
            evidence_summary=evidence_summary,
        )

    def _unknown(
        self,
        projected: float,
        projected_end: Any,
        projection_basis: str,
        freshness: str,
        observed_at: datetime,
        evidence_summary: AdvisorEvidenceSummary,
    ) -> Advisor:
        evidence_trail = EvidenceTrail(
            [
                EvidenceItem(
                    source="Projected bill",
                    observation=f"Projected end-of-bill cost is ${projected:.2f}.",
                    impact=f"Projection uses {projection_basis}, but previous-period comparison data is unavailable.",
                    confidence="Low",
                    freshness=freshness,
                    financial_contribution=FinancialImpact(billing_period=round(projected, 2), basis=projection_basis),
                ),
                EvidenceItem(
                    source="Recommendation",
                    observation="Cost position is unknown",
                    impact="PowerMonitor needs comparable current and previous billing-period data.",
                    confidence="Low",
                    freshness=freshness,
                ),
            ]
        )
        return Advisor(
            advisor_id="cost_advisor",
            advisor_type="COST",
            status="UNKNOWN",
            headline="Cost position needs more comparable data",
            summary="PowerMonitor cannot reliably compare this billing period with the previous period yet.",
            recommendation="Review again after more billing-period data is available.",
            estimated_financial_impact=FinancialImpact(billing_period=round(projected, 2), basis=projection_basis),
            confidence="Low",
            confidence_reason=f"Low confidence because comparison data is unavailable; checked at {observed_at.isoformat(timespec='seconds')}.",
            evidence_trail=evidence_trail,
            freshness=freshness,
            supporting_data_sources=["Financial Engine", "Current billing period"],
            next_review_time=projected_end.isoformat(),
            evidence_summary=evidence_summary,
        )

    def _status(self, difference: float, pct_difference: float, recommendations: list[Decision]) -> str:
        if any(item.id == "trend_bill_trajectory_improving" for item in recommendations):
            return "IMPROVING"
        if difference <= -5 or pct_difference <= -5:
            return "IMPROVING"
        if pct_difference <= 5:
            return "ON_TRACK"
        if pct_difference <= 15:
            return "SLIGHTLY_HIGH"
        return "HIGH"

    def _headline(self, status: str, difference: float, pct_difference: float) -> str:
        if status == "IMPROVING":
            return f"Your bill is tracking ${abs(difference):.0f} below last period."
        if status == "ON_TRACK":
            return "Your bill is on track financially."
        if status == "SLIGHTLY_HIGH":
            return f"Your bill is currently {pct_difference:.0f}% above the previous period."
        if status == "HIGH":
            return f"Your bill is tracking ${difference:.0f} above last period."
        return "Cost position needs more data."

    def _summary(
        self,
        status: str,
        projected: float,
        previous_cost: float,
        difference: float,
        pct_difference: float,
        projected_end: Any,
    ) -> str:
        direction = "below" if difference < 0 else "above"
        if status == "ON_TRACK":
            direction = "within"
            return (
                f"The Financial Engine projects ${projected:.2f} by {projected_end}, "
                f"which is within {abs(pct_difference):.1f}% of last period's ${previous_cost:.2f}."
            )
        return (
            f"The Financial Engine projects ${projected:.2f} by {projected_end}, "
            f"${abs(difference):.2f} {direction} last period's ${previous_cost:.2f}."
        )

    def _recommendation(self, status: str) -> str:
        if status == "IMPROVING":
            return "Keep the current cost pattern and continue watching Peak-period imports."
        if status == "ON_TRACK":
            return "Stay the course and review any high-priority cost recommendations."
        if status == "SLIGHTLY_HIGH":
            return "Review Peak imports and flexible EV charging before the bill closes."
        if status == "HIGH":
            return "Prioritise active billing and tariff recommendations this period."
        return "Review again after more comparable billing data is available."

    def _confidence(self, current_summary: Any, previous_summary: Optional[Any], health: dict[str, Any]) -> str:
        status = str(health.get("status") or "").lower()
        if status not in {"ok"}:
            return "Low"
        if previous_summary is None:
            return "Low"
        supply_days = max(int(self._number(current_summary, "supply_days")), 1)
        reading_days = self._number(current_summary, "reading_days")
        coverage = reading_days / supply_days
        if coverage >= 0.8:
            return "High"
        if coverage >= 0.35:
            return "Medium"
        return "Low"

    def _evidence_summary(
        self,
        *,
        current_summary: Any,
        previous_summary: Optional[Any],
        health: dict[str, Any],
        projection_basis: str,
        confidence: str,
    ) -> AdvisorEvidenceSummary:
        status_ok = str(health.get("status") or "").lower() == "ok"
        supply_days = max(int(self._number(current_summary, "supply_days")), 1)
        reading_days = self._number(current_summary, "reading_days")
        coverage = reading_days / supply_days
        previous_available = previous_summary is not None and self._number(previous_summary, "net_cost") > 0
        complete_period = coverage >= 0.8
        required = [
            EvidenceRequirement(
                "Current billing-period smart-meter data",
                EvidenceSource.SMART_METER,
                status_ok and reading_days > 0,
                quality=EvidenceQuality.HIGH if complete_period else EvidenceQuality.MEDIUM if coverage >= 0.35 else EvidenceQuality.LOW,
                reason=f"{reading_days:.0f} of {supply_days} billing days have readings.",
            ),
            EvidenceRequirement(
                "Financial Engine projection",
                EvidenceSource.DAILY_SUMMARY,
                True,
                quality=EvidenceQuality.HIGH if complete_period else EvidenceQuality.MEDIUM,
                reason=f"Projection basis: {projection_basis}.",
            ),
            EvidenceRequirement(
                "Previous billing-period comparison",
                EvidenceSource.IMPORTED_HISTORY,
                previous_available,
                quality=EvidenceQuality.HIGH if previous_available else EvidenceQuality.LOW,
                reason="Needed to determine whether this period is improving or worsening.",
            ),
        ]
        quality = EvidenceQuality.LOW
        if status_ok and previous_available and complete_period:
            quality = EvidenceQuality.HIGH
        elif status_ok and previous_available and coverage >= 0.35:
            quality = EvidenceQuality.MEDIUM
        capped = cap_confidence_for_evidence(confidence, quality)
        if not complete_period:
            reason = f"Billing period is incomplete: {reading_days:.0f} of {supply_days} days have readings."
        elif not previous_available:
            reason = "Previous billing-period comparison evidence is unavailable."
        else:
            reason = "Current and previous billing-period evidence are available."
        return AdvisorEvidenceSummary(
            required_evidence=required,
            evidence_quality=quality,
            confidence=capped,
            reason=reason,
            confidence_reason=f"{capped} confidence because {reason}",
        )

    def _confidence_reason(self, confidence: str, current_summary: Any, freshness: str, projection_basis: str) -> str:
        supply_days = max(int(self._number(current_summary, "supply_days")), 1)
        reading_days = self._number(current_summary, "reading_days")
        return (
            f"{confidence} confidence because {reading_days:.0f} of {supply_days} billing days have readings, "
            f"data freshness is {freshness.lower()}, and projection basis is {projection_basis}."
        )

    def _freshness(self, current_summary: Any, health: dict[str, Any]) -> str:
        status = str(health.get("status") or "").lower()
        age = health.get("report_age_minutes")
        if status != "ok":
            return status or "unknown"
        if age is not None:
            return f"Report age {float(age):.0f} minutes"
        if int(self._number(current_summary, "intervals")) <= 0:
            return "No interval readings available"
        return "Fresh"

    def _evidence_trail(
        self,
        *,
        projected: float,
        previous_cost: float,
        difference: float,
        pct_difference: float,
        projection_basis: str,
        freshness: str,
        confidence: str,
        current_summary: Any,
        comparison: dict[str, Any],
        recommendations: list[Decision],
        timeline: Optional[DecisionTimeline],
    ) -> EvidenceTrail:
        deltas = comparison.get("deltas", {})
        items = [
            EvidenceItem(
                source="Projected bill",
                observation=f"${projected:.2f} projected end-of-bill cost",
                impact=f"Projection uses {projection_basis}.",
                confidence=confidence,
                freshness=freshness,
                financial_contribution=FinancialImpact(billing_period=round(projected, 2), basis=projection_basis),
            ),
            EvidenceItem(
                source="Previous period comparison",
                observation=f"${abs(difference):.2f} {'below' if difference < 0 else 'above'} previous period",
                impact=f"Difference is {pct_difference:.1f}% against the previous period.",
                confidence=confidence,
                freshness="Previous billing period",
                financial_contribution=FinancialImpact(billing_period=round(difference, 2), basis="projected versus previous"),
            ),
            EvidenceItem(
                source="Import trend",
                observation=f"{deltas.get('grid_import_kwh', 0.0):+.2f} kWh versus comparison period",
                impact="Grid-import movement is part of the bill trajectory.",
                confidence=confidence,
                freshness=freshness,
            ),
            EvidenceItem(
                source="Export trend",
                observation=f"{deltas.get('solar_generated_kwh', 0.0):+.2f} kWh solar generation versus comparison period",
                impact="Solar generation and export influence net cost through avoided imports and export credit.",
                confidence=confidence,
                freshness=freshness,
            ),
            EvidenceItem(
                source="EV contribution",
                observation=f"{self._number(current_summary, 'ev_charging_kwh'):.2f} kWh EV charging this period",
                impact=f"EV charging cost is ${self._number(current_summary, 'ev_charging_cost'):.2f}.",
                confidence=confidence,
                freshness=freshness,
                financial_contribution=FinancialImpact(
                    billing_period=round(self._number(current_summary, "ev_charging_cost"), 2),
                    basis="current billing period EV charging cost",
                ),
            ),
            EvidenceItem(
                source="Tariff contribution",
                observation=f"Peak cost is ${self._number(self._bucket(current_summary, 'peak'), 'cost'):.2f}.",
                impact=f"Peak cost share is {self._number(current_summary, 'peak_cost_share_pct'):.1f}% of import cost.",
                confidence=confidence,
                freshness=freshness,
                financial_contribution=FinancialImpact(
                    billing_period=round(self._number(self._bucket(current_summary, "peak"), "cost"), 2),
                    basis="current billing period Peak tariff cost",
                ),
            ),
        ]
        active_cost_recommendations = [
            item.title for item in recommendations
            if item.category in {"Bills", "Tariffs", "EV"} and item.action_type != "maintain"
        ]
        if active_cost_recommendations:
            items.append(
                EvidenceItem(
                    source="Recommendation Engine",
                    observation=active_cost_recommendations[0],
                    impact="Active recommendation context is included in the financial call.",
                    confidence=confidence,
                    freshness=freshness,
                )
            )
        if timeline and timeline.entries:
            items.append(
                EvidenceItem(
                    source="Timeline",
                    observation=f"{len(timeline.entries)} recent decision events available",
                    impact="Timeline context helps explain whether bill movement is recurring or recent.",
                    confidence=confidence,
                    freshness=freshness,
                )
            )
        return EvidenceTrail(items)

    def _bucket(self, summary: Any, bucket: str) -> dict[str, Any]:
        if isinstance(summary, dict):
            buckets = summary.get("buckets", {})
        else:
            buckets = getattr(summary, "buckets", {})
        return buckets.get(bucket, {}) if isinstance(buckets, dict) else {}

    def _number(self, source: Any, name: str, default: float = 0.0) -> float:
        if source is None:
            return default
        if isinstance(source, dict):
            value = source.get(name, default)
        else:
            value = getattr(source, name, default)
        return float(value or default)
