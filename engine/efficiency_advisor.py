#!/usr/bin/env python3
"""Home efficiency advisor for PowerMonitor."""

from datetime import datetime
from typing import Any, Optional

from .financial_engine import FinancialEngine
from .models import Advisor, Decision, DecisionTimeline, EvidenceItem, EvidenceTrail, FinancialImpact


class EfficiencyAdvisor:
    """Answers where household energy may be wasted without identifying devices."""

    name = "efficiency_advisor"

    def __init__(self, financial_engine: Optional[FinancialEngine] = None) -> None:
        self.financial_engine = financial_engine or FinancialEngine()

    def advise(
        self,
        *,
        summary: Any,
        previous_summary: Optional[Any],
        settings: Any,
        recommendations: list[Decision],
        timeline: Optional[DecisionTimeline],
        health: dict[str, Any],
        overnight_baseload_watts: float,
        comparison: Optional[dict[str, Any]] = None,
        trends: Optional[list[Any]] = None,
        now: Optional[datetime] = None,
    ) -> Advisor:
        observed_at = now or datetime.now().astimezone()
        freshness = self._freshness(summary, health)
        confidence = self._confidence(summary, health, overnight_baseload_watts)
        expected_baseload = self._number(settings, "ev_baseline_watts")
        threshold_pct = self._threshold(settings)
        trigger = expected_baseload * (1 + threshold_pct / 100) if expected_baseload else 0.0
        daily_import = self._number(summary, "average_daily_grid_import_kwh")
        previous_daily_import = self._number(previous_summary, "average_daily_grid_import_kwh") if previous_summary else None
        import_change_pct = self._import_change_pct(daily_import, previous_daily_import)
        excess_watts = max(float(overnight_baseload_watts) - expected_baseload, 0.0)
        impact = self._financial_impact(summary, excess_watts)

        if confidence == "Low" and not overnight_baseload_watts:
            return self._unknown(freshness, observed_at)

        status = self._status(
            overnight_baseload_watts=float(overnight_baseload_watts),
            trigger=trigger,
            import_change_pct=import_change_pct,
            recommendations=recommendations,
        )
        headline = self._headline(status, overnight_baseload_watts, import_change_pct)
        summary_text = self._summary(status, overnight_baseload_watts, expected_baseload, daily_import, import_change_pct)
        recommendation = self._recommendation(status)
        confidence_reason = self._confidence_reason(confidence, summary, freshness, overnight_baseload_watts)
        evidence_trail = self._evidence_trail(
            overnight_baseload_watts=float(overnight_baseload_watts),
            expected_baseload=expected_baseload,
            trigger=trigger,
            daily_import=daily_import,
            previous_daily_import=previous_daily_import,
            import_change_pct=import_change_pct,
            comparison=comparison or {},
            impact=impact,
            confidence=confidence,
            freshness=freshness,
            recommendations=recommendations,
            timeline=timeline,
            trends=trends or [],
        )

        return Advisor(
            advisor_id="efficiency_advisor",
            advisor_type="EFFICIENCY",
            status=status,
            headline=headline,
            summary=summary_text,
            recommendation=recommendation,
            estimated_financial_impact=impact,
            confidence=confidence,
            confidence_reason=confidence_reason,
            evidence_trail=evidence_trail,
            freshness=freshness,
            supporting_data_sources=[
                "Overnight baseload analysis",
                "Daily import trends",
                "Financial Engine",
                "Recommendation Engine",
                "Decision timeline",
            ],
            next_review_time=observed_at.date().isoformat(),
        )

    def _unknown(self, freshness: str, observed_at: datetime) -> Advisor:
        trail = EvidenceTrail(
            [
                EvidenceItem(
                    source="Overnight baseload",
                    observation="No overnight baseload measurement is available.",
                    impact="Efficiency cannot be assessed without overnight interval data.",
                    confidence="Low",
                    freshness=freshness,
                )
            ]
        )
        return Advisor(
            advisor_id="efficiency_advisor",
            advisor_type="EFFICIENCY",
            status="UNKNOWN",
            headline="Efficiency needs more overnight data",
            summary="PowerMonitor cannot reliably assess household efficiency for this period yet.",
            recommendation="Review again after more overnight interval data is available.",
            estimated_financial_impact=None,
            confidence="Low",
            confidence_reason=f"Low confidence because overnight baseload data is unavailable; checked at {observed_at.isoformat(timespec='seconds')}.",
            evidence_trail=trail,
            freshness=freshness,
            supporting_data_sources=["Overnight baseload analysis"],
            next_review_time=observed_at.date().isoformat(),
        )

    def _status(
        self,
        *,
        overnight_baseload_watts: float,
        trigger: float,
        import_change_pct: Optional[float],
        recommendations: list[Decision],
    ) -> str:
        if any(item.id == "baseload_monitoring" for item in recommendations):
            return "INEFFICIENT"
        if import_change_pct is not None and import_change_pct <= -10:
            return "EXCELLENT"
        if trigger and overnight_baseload_watts > trigger:
            return "INEFFICIENT"
        if trigger and overnight_baseload_watts > trigger * 0.9:
            return "WATCH"
        if import_change_pct is not None and import_change_pct <= -3:
            return "GOOD"
        return "GOOD"

    def _headline(self, status: str, overnight_baseload_watts: float, import_change_pct: Optional[float]) -> str:
        if status == "EXCELLENT" and import_change_pct is not None:
            return f"Your home has become {abs(import_change_pct):.0f}% more efficient."
        if status == "GOOD":
            return "No significant efficiency concerns were detected."
        if status == "WATCH":
            return "Overnight baseload is worth watching."
        if status == "INEFFICIENT":
            return "Your overnight baseload is higher than expected."
        return "Efficiency needs more data."

    def _summary(
        self,
        status: str,
        overnight_baseload_watts: float,
        expected_baseload: float,
        daily_import: float,
        import_change_pct: Optional[float],
    ) -> str:
        if status == "INEFFICIENT":
            return (
                f"Your overnight baseload is about {overnight_baseload_watts:.0f} W versus the "
                f"configured {expected_baseload:.0f} W baseline. No specific device is inferred."
            )
        if import_change_pct is not None and import_change_pct < 0:
            return f"Daily grid import is {abs(import_change_pct):.1f}% lower than the comparison period."
        return f"Average daily grid import is {daily_import:.1f} kWh with no major efficiency concern detected."

    def _recommendation(self, status: str) -> str:
        if status == "INEFFICIENT":
            return "Review measured overnight load patterns and reduce avoidable always-on consumption where practical."
        if status == "WATCH":
            return "Keep an eye on overnight baseload before it becomes a recurring concern."
        if status == "EXCELLENT":
            return "Keep reinforcing the behaviours behind the lower daily import pattern."
        if status == "GOOD":
            return "Stay the course and review future baseload recommendations if they appear."
        return "Review again after more overnight interval data is available."

    def _financial_impact(self, summary: Any, excess_watts: float) -> FinancialImpact:
        if excess_watts <= 0:
            return FinancialImpact(billing_period=0.0, basis="No excess baseload above configured baseline")
        hours = 8.0 * self._number(summary, "supply_days")
        excess_kwh = excess_watts / 1000.0 * hours
        average_rate = self.financial_engine.weighted_average_import_rate(summary)
        cost = excess_kwh * average_rate
        return FinancialImpact(
            billing_period=round(cost, 2),
            basis="Measured excess overnight baseload above configured baseline",
        )

    def _confidence(self, summary: Any, health: dict[str, Any], overnight_baseload_watts: float) -> str:
        if str(health.get("status") or "").lower() != "ok":
            return "Low"
        if self._number(summary, "intervals") <= 0 or not overnight_baseload_watts:
            return "Low"
        supply_days = max(int(self._number(summary, "supply_days")), 1)
        coverage = self._number(summary, "reading_days") / supply_days
        if coverage >= 0.8:
            return "High"
        if coverage >= 0.35:
            return "Medium"
        return "Low"

    def _confidence_reason(self, confidence: str, summary: Any, freshness: str, overnight_baseload_watts: float) -> str:
        return (
            f"{confidence} confidence because overnight baseload is {overnight_baseload_watts:.0f} W, "
            f"{self._number(summary, 'reading_days'):.0f} billing days have readings, and freshness is {freshness.lower()}."
        )

    def _freshness(self, summary: Any, health: dict[str, Any]) -> str:
        status = str(health.get("status") or "").lower()
        age = health.get("report_age_minutes")
        if status != "ok":
            return status or "unknown"
        if age is not None:
            return f"Report age {float(age):.0f} minutes"
        if self._number(summary, "intervals") <= 0:
            return "No interval readings available"
        return "Fresh"

    def _evidence_trail(
        self,
        *,
        overnight_baseload_watts: float,
        expected_baseload: float,
        trigger: float,
        daily_import: float,
        previous_daily_import: Optional[float],
        import_change_pct: Optional[float],
        comparison: dict[str, Any],
        impact: FinancialImpact,
        confidence: str,
        freshness: str,
        recommendations: list[Decision],
        timeline: Optional[DecisionTimeline],
        trends: list[Any],
    ) -> EvidenceTrail:
        deltas = comparison.get("deltas", {})
        items = [
            EvidenceItem(
                source="Overnight baseload",
                observation=f"{overnight_baseload_watts:.0f} W typical overnight baseload",
                impact=f"Configured baseline is {expected_baseload:.0f} W; watch threshold is {trigger:.0f} W.",
                confidence=confidence,
                freshness=freshness,
                financial_contribution=impact,
            ),
            EvidenceItem(
                source="Daily average import",
                observation=f"{daily_import:.2f} kWh/day average grid import",
                impact="Daily import helps show whether overall household efficiency is changing.",
                confidence=confidence,
                freshness=freshness,
            ),
            EvidenceItem(
                source="Trend compared with previous billing period",
                observation=(
                    f"{import_change_pct:+.1f}% daily import change"
                    if import_change_pct is not None
                    else "Previous-period daily import comparison unavailable"
                ),
                impact=f"Grid import delta is {deltas.get('grid_import_kwh', 0.0):+.2f} kWh.",
                confidence=confidence,
                freshness="Previous billing period" if previous_daily_import is not None else freshness,
            ),
            EvidenceItem(
                source="Financial impact estimate",
                observation=(
                    f"${impact.billing_period:.2f} estimated excess baseload cost"
                    if impact.billing_period is not None
                    else "Financial impact not quantified"
                ),
                impact=impact.basis or "Measured efficiency impact.",
                confidence=confidence,
                freshness=freshness,
                financial_contribution=impact,
            ),
        ]
        baseload_recommendations = [item.title for item in recommendations if item.id == "baseload_monitoring" or item.category == "Behaviour"]
        if baseload_recommendations:
            items.append(
                EvidenceItem(
                    source="Recommendation context",
                    observation=baseload_recommendations[0],
                    impact="Recommendation context is included without identifying specific devices.",
                    confidence=confidence,
                    freshness=freshness,
                )
            )
        if trends:
            items.append(
                EvidenceItem(
                    source="Daily import trends",
                    observation=f"{len(trends)} daily trend records available",
                    impact="Trend records support the daily import context.",
                    confidence=confidence,
                    freshness=freshness,
                )
            )
        if timeline and timeline.entries:
            items.append(
                EvidenceItem(
                    source="Timeline evidence",
                    observation=f"{len(timeline.entries)} recent decision events available",
                    impact="Timeline context shows whether efficiency signals are recurring or recent.",
                    confidence=confidence,
                    freshness=freshness,
                )
            )
        return EvidenceTrail(items)

    def _threshold(self, settings: Any) -> float:
        thresholds = getattr(settings, "recommendation_thresholds", None)
        if isinstance(settings, dict):
            thresholds = settings.get("recommendation_thresholds", thresholds)
        if isinstance(thresholds, dict):
            return float(thresholds.get("baseload_excess_pct", 20.0))
        return 20.0

    def _import_change_pct(self, daily_import: float, previous_daily_import: Optional[float]) -> Optional[float]:
        if previous_daily_import is None or previous_daily_import <= 0:
            return None
        return (daily_import - previous_daily_import) / previous_daily_import * 100

    def _number(self, source: Any, name: str, default: float = 0.0) -> float:
        if source is None:
            return default
        if isinstance(source, dict):
            value = source.get(name, default)
        else:
            value = getattr(source, name, default)
        return float(value or default)
