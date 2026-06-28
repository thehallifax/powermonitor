#!/usr/bin/env python3
"""Solar utilisation advisor for PowerMonitor."""

from datetime import datetime
from typing import Any, Optional

from .financial_engine import FinancialEngine
from .models import Advisor, Decision, DecisionTimeline, EvidenceItem, EvidenceTrail, FinancialImpact, HouseholdContext


class SolarAdvisor:
    """Answers whether the household is making the most of solar generation."""

    name = "solar_advisor"

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
        summary: Any,
        settings: Any,
        recommendations: list[Decision],
        timeline: Optional[DecisionTimeline],
        health: dict[str, Any],
        now: Optional[datetime] = None,
    ) -> Advisor:
        observed_at = now or datetime.now().astimezone()
        freshness = self._freshness(summary, health)
        confidence = self._confidence(summary, health)
        generated = self._number(summary, "solar_generated_kwh")
        used = self._number(summary, "solar_used_kwh")
        exported = self._number(summary, "solar_exported_kwh")
        imported = self._number(summary, "grid_import_kwh")
        self_consumption = self._number(summary, "self_consumption_pct")
        export_pct = self._number(summary, "solar_export_pct")
        export_credit = self.financial_engine.calculate_export_credit(
            exported,
            self._number(settings, "export_credit_rate"),
        )
        average_import_rate = self.financial_engine.weighted_average_import_rate(summary)
        potential_import_offset = min(exported, imported) * average_import_rate
        net_opportunity = max(potential_import_offset - export_credit, 0.0)

        if confidence == "Low" and generated <= 0:
            return self._unknown(freshness, observed_at)

        status = self._status(self_consumption, export_pct, exported, recommendations)
        headline = self._headline(status, self_consumption, exported)
        summary_text = self._summary(status, generated, used, exported, self_consumption)
        recommendation = self._recommendation(status)
        impact = FinancialImpact(
            billing_period=round(net_opportunity, 2),
            basis="Estimated import cost offset if exported solar had instead displaced grid imports",
        )
        confidence_reason = self._confidence_reason(confidence, summary, freshness)
        evidence_trail = self._evidence_trail(
            generated=generated,
            used=used,
            exported=exported,
            imported=imported,
            self_consumption=self_consumption,
            export_credit=export_credit,
            potential_import_offset=potential_import_offset,
            net_opportunity=net_opportunity,
            freshness=freshness,
            confidence=confidence,
            recommendations=recommendations,
            timeline=timeline,
        )

        return Advisor(
            advisor_id="solar_advisor",
            advisor_type="SOLAR",
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
                "SolarEdge interval data",
                "Financial Engine",
                "Recommendation Engine",
                "Decision timeline",
            ],
            next_review_time=observed_at.date().isoformat(),
        )

    def _unknown(self, freshness: str, observed_at: datetime) -> Advisor:
        evidence_trail = EvidenceTrail(
            [
                EvidenceItem(
                    source="Solar data",
                    observation="No solar generation is available for the selected period.",
                    impact="Solar utilisation cannot be assessed without generation, usage, or export values.",
                    confidence="Low",
                    freshness=freshness,
                )
            ]
        )
        return Advisor(
            advisor_id="solar_advisor",
            advisor_type="SOLAR",
            status="UNKNOWN",
            headline="Solar usage needs more data",
            summary="PowerMonitor cannot reliably assess solar utilisation for this period yet.",
            recommendation="Review again after more SolarEdge interval data is available.",
            estimated_financial_impact=None,
            confidence="Low",
            confidence_reason=f"Low confidence because solar interval evidence is unavailable; checked at {observed_at.isoformat(timespec='seconds')}.",
            evidence_trail=evidence_trail,
            freshness=freshness,
            supporting_data_sources=["SolarEdge interval data"],
            next_review_time=observed_at.date().isoformat(),
        )

    def _status(
        self,
        self_consumption: float,
        export_pct: float,
        exported: float,
        recommendations: list[Decision],
    ) -> str:
        if any(item.id == "trend_recurring_solar_utilisation_win" for item in recommendations):
            return "EXCELLENT"
        if self_consumption >= 75:
            return "EXCELLENT"
        if self_consumption >= 60:
            return "GOOD"
        if exported >= 8 or export_pct >= 45:
            return "MISSED_OPPORTUNITY"
        return "OPPORTUNITY"

    def _headline(self, status: str, self_consumption: float, exported: float) -> str:
        if status == "EXCELLENT":
            return "You are making excellent use of your solar."
        if status == "GOOD":
            return f"Your household used {self_consumption:.0f}% of solar production."
        if status == "MISSED_OPPORTUNITY":
            return f"You exported {exported:.1f} kWh that could potentially have powered flexible loads."
        if status == "OPPORTUNITY":
            return "There is room to use more solar at home."
        return "Solar usage needs more data."

    def _summary(
        self,
        status: str,
        generated: float,
        used: float,
        exported: float,
        self_consumption: float,
    ) -> str:
        if status in {"EXCELLENT", "GOOD"}:
            return (
                f"Your household used {self_consumption:.1f}% of {generated:.1f} kWh solar production "
                f"and exported {exported:.1f} kWh."
            )
        return (
            f"You generated {generated:.1f} kWh, used {used:.1f} kWh directly, and exported {exported:.1f} kWh. "
            "This is period-level export evidence, not live instantaneous surplus."
        )

    def _recommendation(self, status: str) -> str:
        if status == "EXCELLENT":
            return "Keep the current solar usage pattern."
        if status == "GOOD":
            return "Keep prioritising daytime flexible loads where practical."
        if status == "MISSED_OPPORTUNITY":
            return "Review active solar recommendations and consider moving flexible loads into sunny periods."
        if status == "OPPORTUNITY":
            return "Look for flexible loads that can run during solar generation periods."
        return "Review again after more solar data is available."

    def _confidence(self, summary: Any, health: dict[str, Any]) -> str:
        status = str(health.get("status") or "").lower()
        if status not in {"ok"}:
            return "Low"
        intervals = self._number(summary, "intervals")
        generated = self._number(summary, "solar_generated_kwh")
        if intervals <= 0 or generated <= 0:
            return "Low"
        supply_days = max(int(self._number(summary, "supply_days")), 1)
        reading_days = self._number(summary, "reading_days")
        coverage = reading_days / supply_days
        if coverage >= 0.8:
            return "High"
        if coverage >= 0.35:
            return "Medium"
        return "Low"

    def _confidence_reason(self, confidence: str, summary: Any, freshness: str) -> str:
        return (
            f"{confidence} confidence because solar utilisation uses {self._number(summary, 'reading_days'):.0f} "
            f"reading days and data freshness is {freshness.lower()}."
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
        generated: float,
        used: float,
        exported: float,
        imported: float,
        self_consumption: float,
        export_credit: float,
        potential_import_offset: float,
        net_opportunity: float,
        freshness: str,
        confidence: str,
        recommendations: list[Decision],
        timeline: Optional[DecisionTimeline],
    ) -> EvidenceTrail:
        items = [
            EvidenceItem(
                source="Solar generated",
                observation=f"{generated:.2f} kWh generated",
                impact="Total solar production available to use, export, or offset imports.",
                confidence=confidence,
                freshness=freshness,
            ),
            EvidenceItem(
                source="Solar self-consumption",
                observation=f"{self_consumption:.1f}% used directly at home",
                impact=f"{used:.2f} kWh of solar production was consumed on site.",
                confidence=confidence,
                freshness=freshness,
            ),
            EvidenceItem(
                source="Exported energy",
                observation=f"{exported:.2f} kWh exported",
                impact="Exported energy is a period-level opportunity signal, not live surplus.",
                confidence=confidence,
                freshness=freshness,
            ),
            EvidenceItem(
                source="Estimated export credit",
                observation=f"${export_credit:.2f} export credit",
                impact="Export credit reduces the bill but is usually lower than avoided import cost.",
                confidence=confidence,
                freshness=freshness,
                financial_contribution=FinancialImpact(billing_period=round(export_credit, 2), basis="export credit"),
            ),
            EvidenceItem(
                source="Import offset",
                observation=f"Up to ${potential_import_offset:.2f} import cost offset from exported energy",
                impact="Uses existing import cost and export data only; no appliance behaviour is inferred.",
                confidence=confidence,
                freshness=freshness,
                financial_contribution=FinancialImpact(billing_period=round(potential_import_offset, 2), basis="exported kWh at average import rate"),
            ),
            EvidenceItem(
                source="Billing contribution",
                observation=f"${net_opportunity:.2f} estimated solar-use opportunity",
                impact="Difference between potential import offset and export credit.",
                confidence=confidence,
                freshness=freshness,
                financial_contribution=FinancialImpact(billing_period=round(net_opportunity, 2), basis="import offset minus export credit"),
            ),
        ]
        solar_recommendations = [item.title for item in recommendations if item.category == "Solar"]
        if solar_recommendations:
            items.append(
                EvidenceItem(
                    source="Recommendation Engine",
                    observation=solar_recommendations[0],
                    impact="Active solar recommendation context is included in the advisor call.",
                    confidence=confidence,
                    freshness=freshness,
                )
            )
        if timeline and timeline.entries:
            items.append(
                EvidenceItem(
                    source="Timeline",
                    observation=f"{len(timeline.entries)} recent decision events available",
                    impact="Timeline context helps show whether solar utilisation signals are recurring.",
                    confidence=confidence,
                    freshness=freshness,
                )
            )
        return EvidenceTrail(items)

    def _number(self, source: Any, name: str, default: float = 0.0) -> float:
        if source is None:
            return default
        if isinstance(source, dict):
            value = source.get(name, default)
        else:
            value = getattr(source, name, default)
        return float(value or default)
