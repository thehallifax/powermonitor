#!/usr/bin/env python3
"""Shared EV charging advisor for PowerMonitor.

The Charging Advisor combines existing tariff, solar, bill, recommendation,
decision, EV history, and freshness evidence. It does not render presentation
markup and it does not forecast future appliance behaviour.
"""

from datetime import datetime, time, timedelta
from typing import Any, Optional

from .evidence_quality import (
    AdvisorEvidenceSummary,
    EvidenceQuality,
    EvidenceRequirement,
    EvidenceSource,
    cap_confidence_for_evidence,
)
from .context_engine import (
    ContextEngine,
    HouseholdContext as CurrentHouseholdContext,
    TARIFF_LABELS,
    TariffWindow,
    current_tariff_bucket as context_current_tariff_bucket,
    lowest_cost_window as context_lowest_cost_window,
    next_lower_cost_window as context_next_lower_cost_window,
    upcoming_tariff_windows as context_upcoming_tariff_windows,
    wait_minutes as context_wait_minutes,
)
from .models import Advisor, ChargingAdvice, Decision, EvidenceItem, EvidenceTrail, FinancialImpact, HouseholdContext as PlatformHouseholdContext
EV_BUCKET_LABELS = {
    "super_off_peak": "9am-3pm super off-peak / EV solar window",
    "overnight": "11pm-6am overnight",
    "off_peak": "off-peak",
    "peak": "peak",
}


class ChargingAdvisor:
    """Deterministic advisor for the question: should I charge my EV right now?"""

    name = "charging_advisor"

    def __init__(self, household_context: Optional[PlatformHouseholdContext] = None) -> None:
        self.household_context = household_context

    def advise(
        self,
        *,
        today_summary: Any,
        billing_summary: Any,
        settings: Any,
        health: dict[str, Any],
        recommendations: list[Decision],
        projected_bill: Optional[float] = None,
        now: Optional[datetime] = None,
        context: Optional[CurrentHouseholdContext] = None,
    ) -> ChargingAdvice:
        observed_at = now or datetime.now().astimezone()
        context = context or ContextEngine().build(
            today_summary=today_summary,
            billing_summary=billing_summary,
            settings=settings,
            health=health,
            now=observed_at,
        )
        bucket = context.current_tariff_bucket or current_tariff_bucket(observed_at)
        available, freshness, confidence = self._data_is_available(today_summary, billing_summary, health)
        solar_status, has_solar_surplus = solar_signal(today_summary)
        has_live_context = "Provider observations" in set(context.data_sources_used)
        if has_live_context and context.solar.value == "SOLAR_SURPLUS_POSSIBLE":
            solar_status = "Live solar surplus detected"
            has_solar_surplus = True
        elif has_live_context and context.solar.value == "SOLAR_AVAILABLE":
            solar_status = "Live solar production detected"
        if not context.daylight:
            has_solar_surplus = False
            if solar_status != "Solar unavailable":
                solar_status = "Solar is not available at night"
        tariff_rates = self._import_rates(settings)
        top_priority = self._top_priority(recommendations)
        bill_is_high = any(item.id == "high_cost_alert" for item in recommendations)
        tariff_now_rate = tariff_rates.get(bucket)
        current_window = TariffWindow(bucket, TARIFF_LABELS[bucket], observed_at, tariff_now_rate)
        next_lower_window = next_lower_cost_window(observed_at, bucket, tariff_rates)
        lowest_window = lowest_cost_window(observed_at, bucket, tariff_rates)
        next_bucket = next_lower_window.bucket
        next_window = next_lower_window.display(observed_at)
        next_window_start = next_lower_window.start
        tariff_next_rate = next_lower_window.rate
        per_kwh_difference = self._per_kwh_difference(bucket, next_bucket, tariff_rates)
        recommended_wait_minutes = wait_minutes(observed_at, next_lower_window.start)
        per_kwh_benefit = self._per_kwh_benefit(bucket, tariff_rates)
        charger_data_status, ev_session_freshness = self._charger_data_status(billing_summary)
        solar_data_status = self._solar_data_status(today_summary, freshness)
        evidence_summary = self._evidence_summary(
            today_summary=today_summary,
            billing_summary=billing_summary,
            health=health,
            tariff_rates=tariff_rates,
            confidence=confidence,
            freshness=freshness,
            charger_data_status=charger_data_status,
            solar_data_status=solar_data_status,
            current_bucket=bucket,
            daily_export_supports_current_surplus=has_solar_surplus,
        )
        confidence = evidence_summary.confidence
        confidence_reason = evidence_summary.confidence_reason or self._confidence_reason(confidence, freshness, charger_data_status, solar_data_status)

        evidence = {
            "current_tariff_bucket": bucket,
            "current_tariff_label": TARIFF_LABELS[bucket],
            "current_import_rate": tariff_now_rate,
            "super_off_peak_rate": tariff_rates.get("super_off_peak"),
            "next_tariff_bucket": next_bucket,
            "next_tariff_label": TARIFF_LABELS[next_bucket],
            "next_import_rate": tariff_next_rate,
            "lowest_tariff_bucket": lowest_window.bucket,
            "lowest_tariff_label": lowest_window.label,
            "lowest_import_rate": lowest_window.rate,
            "recommended_wait_minutes": recommended_wait_minutes,
            "per_kwh_difference": per_kwh_difference,
            "solar_generated_kwh_today": self._number(today_summary, "solar_generated_kwh"),
            "solar_exported_kwh_today": self._number(today_summary, "solar_exported_kwh"),
            "solar_export_pct_today": self._number(today_summary, "solar_export_pct"),
            "solar_status": solar_status,
            "live_solar_context": context.solar.value,
            "live_charging_context": context.charging.value,
            "ev_charging_kwh_billing_period": self._number(billing_summary, "ev_charging_kwh"),
            "ev_charging_source": getattr(billing_summary, "ev_charging_source", None),
            "projected_bill": projected_bill,
            "bill_is_high": bill_is_high,
            "top_recommendation_priority": top_priority,
            "health_status": health.get("status"),
            "report_age_minutes": health.get("report_age_minutes"),
            "context": context.as_dict(),
        }

        if not available:
            headline = "Charging advice needs fresher data"
            explanation = (
                "PowerMonitor does not have fresh enough interval data to make a reliable charging call. "
                f"The tariff schedule is still known, but solar, billing, and EV evidence are {freshness.lower()}."
            )
            evidence_trail = self._evidence_trail(
                bucket=bucket,
                next_bucket=next_bucket,
                tariff_now_rate=tariff_now_rate,
                tariff_next_rate=tariff_next_rate,
                per_kwh_difference=per_kwh_difference,
                freshness=freshness,
                solar_status=solar_status,
                solar_data_status=solar_data_status,
                ev_session_freshness=ev_session_freshness,
                bill_is_high=bill_is_high,
                top_priority=top_priority,
                status="UNKNOWN",
                headline=headline,
                benefit=None,
                advice_confidence="Low",
            )
            advisor = self._advisor(
                status="UNKNOWN",
                headline=headline,
                summary=explanation,
                recommendation=headline,
                benefit=None,
                confidence="Low",
                confidence_reason=confidence_reason,
                evidence_trail=evidence_trail,
                freshness=freshness,
                supporting_data_sources=self._supporting_data_sources(charger_data_status, solar_data_status),
                next_review_time=next_window_start.isoformat(timespec="seconds") if next_window_start else None,
                evidence_summary=evidence_summary,
            )
            return ChargingAdvice(
                status="UNKNOWN",
                headline=headline,
                explanation=explanation,
                estimated_financial_benefit=None,
                confidence="Low",
                supporting_evidence=evidence,
                next_recommended_charging_window=next_window,
                data_freshness=freshness,
                current_window=EV_BUCKET_LABELS[bucket],
                current_window_label=current_window.label,
                current_window_rate=current_window.rate,
                next_lower_cost_window=next_window,
                next_lower_cost_window_start=next_window_start.isoformat(timespec="seconds") if next_window_start else None,
                next_lower_cost_window_label=next_lower_window.label,
                next_lower_cost_window_rate=next_lower_window.rate,
                lowest_cost_window=lowest_window.display(observed_at),
                lowest_cost_window_start=lowest_window.start.isoformat(timespec="seconds") if lowest_window.start else None,
                lowest_cost_window_label=lowest_window.label,
                lowest_cost_window_rate=lowest_window.rate,
                recommended_wait_minutes=recommended_wait_minutes,
                next_window=next_window,
                next_window_start=next_window_start.isoformat(timespec="seconds") if next_window_start else None,
                tariff_now_label=TARIFF_LABELS[bucket],
                tariff_now_rate=tariff_now_rate,
                tariff_next_label=TARIFF_LABELS[next_bucket],
                tariff_next_rate=tariff_next_rate,
                per_kwh_difference=per_kwh_difference,
                charger_data_status=charger_data_status,
                ev_session_freshness=ev_session_freshness,
                solar_data_status=solar_data_status,
                confidence_reason=confidence_reason,
                evidence_trail=evidence_trail,
                advisor=advisor,
            )

        benefit = self._financial_benefit(bucket, per_kwh_benefit)
        if bucket == "super_off_peak":
            headline = "Charge now in the cheapest window"
            explanation = (
                f"Advice is tariff-based: the current window is Super off peak at ${tariff_now_rate:.4f}/kWh, "
                "the lowest configured import rate."
            )
            if has_solar_surplus:
                explanation += " Daily solar data also shows export today, so charging now is supported by solar utilisation evidence."
            status = "CHARGE_NOW"
        elif has_solar_surplus:
            headline = "Wait for live solar evidence or a cheaper tariff window"
            explanation = (
                "Today's export shows solar was available earlier, but it cannot prove surplus solar exists right now. "
                f"Because the current tariff is {TARIFF_LABELS[bucket]}, wait for live solar evidence or the next lower-cost window."
            )
            status = "WAIT"
        elif bucket == "peak":
            headline = "Avoid charging during Peak"
            explanation = (
                f"Advice is tariff-based: the current Peak rate is ${tariff_now_rate:.4f}/kWh. "
                f"The next cheaper window is {next_window} at ${tariff_next_rate:.4f}/kWh. "
                f"The lowest-cost window is {lowest_window.display(observed_at)} at ${lowest_window.rate:.4f}/kWh."
            )
            status = "AVOID"
        else:
            headline = "Wait for a cheaper charging window"
            explanation = (
                f"Advice is tariff-based: the current {TARIFF_LABELS[bucket]} rate is ${tariff_now_rate:.4f}/kWh. "
                f"The next cheaper window is {next_window} at ${tariff_next_rate:.4f}/kWh. "
                f"The lowest-cost window is {lowest_window.display(observed_at)} at ${lowest_window.rate:.4f}/kWh."
            )
            status = "WAIT"

        if bill_is_high and status != "CHARGE_NOW":
            explanation += " The current billing position is already flagged as high, so avoiding avoidable charging cost matters more."
        if top_priority is not None and top_priority >= 67:
            evidence["recommendation_pressure"] = "high"
        evidence_trail = self._evidence_trail(
            bucket=bucket,
            next_bucket=next_bucket,
            tariff_now_rate=tariff_now_rate,
            tariff_next_rate=tariff_next_rate,
            per_kwh_difference=per_kwh_difference,
            freshness=freshness,
            solar_status=solar_status,
            solar_data_status=solar_data_status,
            ev_session_freshness=ev_session_freshness,
            bill_is_high=bill_is_high,
            top_priority=top_priority,
            status=status,
            headline=headline,
            benefit=benefit,
            advice_confidence=confidence,
        )
        advisor = self._advisor(
            status=status,
            headline=headline,
            summary=explanation,
            recommendation=headline,
            benefit=benefit,
            confidence=confidence,
            confidence_reason=confidence_reason,
            evidence_trail=evidence_trail,
            freshness=freshness,
            supporting_data_sources=self._supporting_data_sources(charger_data_status, solar_data_status),
            next_review_time=next_window_start.isoformat(timespec="seconds") if next_window_start else None,
            evidence_summary=evidence_summary,
        )

        return ChargingAdvice(
            status=status,
            headline=headline,
            explanation=explanation,
            estimated_financial_benefit=benefit,
            confidence=confidence,
            supporting_evidence=evidence,
            next_recommended_charging_window=next_window,
            data_freshness=freshness,
            current_window=EV_BUCKET_LABELS[bucket],
            current_window_label=current_window.label,
            current_window_rate=current_window.rate,
            next_lower_cost_window=next_window,
            next_lower_cost_window_start=next_window_start.isoformat(timespec="seconds") if next_window_start else None,
            next_lower_cost_window_label=next_lower_window.label,
            next_lower_cost_window_rate=next_lower_window.rate,
            lowest_cost_window=lowest_window.display(observed_at),
            lowest_cost_window_start=lowest_window.start.isoformat(timespec="seconds") if lowest_window.start else None,
            lowest_cost_window_label=lowest_window.label,
            lowest_cost_window_rate=lowest_window.rate,
            recommended_wait_minutes=recommended_wait_minutes,
            next_window=next_window,
            next_window_start=next_window_start.isoformat(timespec="seconds") if next_window_start else None,
            tariff_now_label=TARIFF_LABELS[bucket],
            tariff_now_rate=tariff_now_rate,
            tariff_next_label=TARIFF_LABELS[next_bucket],
            tariff_next_rate=tariff_next_rate,
            per_kwh_difference=per_kwh_difference,
            charger_data_status=charger_data_status,
            ev_session_freshness=ev_session_freshness,
            solar_data_status=solar_data_status,
            confidence_reason=confidence_reason,
            evidence_trail=evidence_trail,
            advisor=advisor,
        )

    def _data_is_available(self, today_summary: Any, billing_summary: Any, health: dict[str, Any]) -> tuple[bool, str, str]:
        status = str(health.get("status") or "").lower()
        intervals = int(getattr(today_summary, "intervals", 0) or 0)
        billing_intervals = int(getattr(billing_summary, "intervals", 0) or 0)
        if status in {"no readings", "invalid reading timestamp", "solaredge session expired"}:
            return False, status or "unavailable", "Low"
        if status not in {"ok"}:
            return False, status or "unavailable", "Low"
        if intervals <= 0 and billing_intervals <= 0:
            return False, "No interval readings available", "Low"
        if intervals <= 0:
            return False, "No readings for today yet", "Low"
        age = health.get("report_age_minutes")
        if age is not None and float(age) > 180:
            return False, f"Report age {float(age):.0f} minutes", "Low"
        if age is not None and float(age) > 60:
            return True, f"Report age {float(age):.0f} minutes", "Medium"
        return True, "Fresh", "High"

    def _import_rates(self, settings: Any) -> dict[str, float]:
        rates = settings.get("import_rates", {}) if isinstance(settings, dict) else getattr(settings, "import_rates", {})
        return {bucket: float(value) for bucket, value in rates.items()}

    def _number(self, source: Any, name: str) -> float:
        if isinstance(source, dict):
            value = source.get(name, 0.0)
        else:
            value = getattr(source, name, 0.0)
        return float(value or 0.0)

    def _top_priority(self, recommendations: list[Decision]) -> Optional[int]:
        scores = []
        for recommendation in recommendations:
            score = recommendation.evidence.get("priority_score")
            if score is not None:
                scores.append(int(score))
        return max(scores) if scores else None

    def _per_kwh_benefit(self, bucket: str, rates: dict[str, float]) -> Optional[float]:
        current = rates.get(bucket)
        super_off_peak = rates.get("super_off_peak")
        peak = rates.get("peak")
        if current is None or super_off_peak is None:
            return None
        if bucket == "super_off_peak" and peak is not None:
            return max(peak - super_off_peak, 0.0)
        return max(current - super_off_peak, 0.0)

    def _per_kwh_difference(self, bucket: str, next_bucket: str, rates: dict[str, float]) -> Optional[float]:
        current = rates.get(bucket)
        next_rate = rates.get(next_bucket)
        if current is None or next_rate is None:
            return None
        return round(max(current - next_rate, 0.0), 4)

    def _financial_benefit(self, bucket: str, per_kwh_benefit: Optional[float]) -> Optional[FinancialImpact]:
        if per_kwh_benefit is None:
            return None
        if per_kwh_benefit <= 0 and bucket != "super_off_peak":
            return None
        basis = (
            "per kWh difference between Peak and Super off peak"
            if bucket == "super_off_peak"
            else "per kWh difference between current tariff and Super off peak"
        )
        return FinancialImpact(today=round(per_kwh_benefit, 4), currency="AUD", basis=basis)

    def _charger_data_status(self, billing_summary: Any) -> tuple[str, str]:
        source = getattr(billing_summary, "ev_charging_source", None)
        if isinstance(billing_summary, dict):
            source = billing_summary.get("ev_charging_source")
        if source in {"ocular_export", "home_assistant_ocpp"}:
            return "actual_session_data_available", "Actual EV session data is available for the billing period."
        if source in {"estimated", None, ""}:
            return "estimated_fallback", "EV charging uses the overnight fallback estimate because actual sessions are unavailable."
        return str(source), "EV charging data source is configured but not recognised as actual session telemetry."

    def _solar_data_status(self, today_summary: Any, freshness: str) -> str:
        generated = self._number(today_summary, "solar_generated_kwh")
        intervals = int(getattr(today_summary, "intervals", 0) or 0)
        if intervals <= 0:
            return "No solar interval data is available for today."
        if generated <= 0:
            return "Today's solar summary is available but shows no generation yet."
        return f"Today's solar summary is available from interval data; freshness: {freshness}."

    def _confidence_reason(
        self,
        confidence: str,
        freshness: str,
        charger_data_status: str,
        solar_data_status: str,
    ) -> str:
        if confidence == "Low":
            return f"Low confidence because the available source data is {freshness.lower()}."
        if confidence == "Medium":
            return f"Medium confidence because tariff data is configured but report freshness is {freshness.lower()}."
        if charger_data_status == "estimated_fallback":
            return "High confidence for tariff timing, with EV history limited by estimated charging data."
        if "No solar interval" in solar_data_status:
            return "High confidence for tariff timing, with solar context unavailable today."
        return "High confidence because tariff timing and today's interval data are fresh."

    def _supporting_data_sources(self, charger_data_status: str, solar_data_status: str) -> list[str]:
        sources = ["Configured tariff schedule", "Financial Engine", "Recommendation Engine"]
        if "No solar interval" not in solar_data_status:
            sources.append("SolarEdge interval data")
        if charger_data_status == "actual_session_data_available":
            sources.append("EV charging session data")
        else:
            sources.append("Estimated EV charging")
        return sources

    def _advisor(
        self,
        *,
        status: str,
        headline: str,
        summary: str,
        recommendation: str,
        benefit: Optional[FinancialImpact],
        confidence: str,
        confidence_reason: str,
        evidence_trail: EvidenceTrail,
        freshness: str,
        supporting_data_sources: list[str],
        next_review_time: Optional[str],
        evidence_summary: AdvisorEvidenceSummary,
    ) -> Advisor:
        return Advisor(
            advisor_id="charging_advisor",
            advisor_type="EV_CHARGING",
            status=status,
            headline=headline,
            summary=summary,
            recommendation=recommendation,
            estimated_financial_impact=benefit,
            confidence=confidence,
            confidence_reason=confidence_reason,
            evidence_trail=evidence_trail,
            freshness=freshness,
            supporting_data_sources=supporting_data_sources,
            next_review_time=next_review_time,
            evidence_summary=evidence_summary,
        )

    def _evidence_summary(
        self,
        *,
        today_summary: Any,
        billing_summary: Any,
        health: dict[str, Any],
        tariff_rates: dict[str, float],
        confidence: str,
        freshness: str,
        charger_data_status: str,
        solar_data_status: str,
        current_bucket: str,
        daily_export_supports_current_surplus: bool,
    ) -> AdvisorEvidenceSummary:
        intervals = int(self._number(today_summary, "intervals"))
        billing_intervals = int(self._number(billing_summary, "intervals"))
        health_ok = str(health.get("status") or "").lower() == "ok"
        tariff_available = bool(tariff_rates.get("peak") and tariff_rates.get("super_off_peak"))
        live_solar_available = False
        ev_actual = charger_data_status == "actual_session_data_available"
        required = [
            EvidenceRequirement(
                "Configured tariff schedule",
                EvidenceSource.USER_CONFIGURATION,
                tariff_available,
                quality=EvidenceQuality.HIGH if tariff_available else EvidenceQuality.UNKNOWN,
                reason="Current and next tariff windows drive charging timing.",
            ),
            EvidenceRequirement(
                "Fresh smart-meter interval data",
                EvidenceSource.SMART_METER,
                health_ok and (intervals > 0 or billing_intervals > 0),
                quality=EvidenceQuality.HIGH if freshness == "Fresh" else EvidenceQuality.MEDIUM if "Report age" in freshness else EvidenceQuality.LOW,
                reason=f"Freshness: {freshness}.",
            ),
        ]
        optional = [
            EvidenceRequirement(
                "Live solar surplus",
                EvidenceSource.LIVE_TELEMETRY if live_solar_available else EvidenceSource.MISSING,
                live_solar_available,
                required=False,
                quality=EvidenceQuality.HIGH if live_solar_available else EvidenceQuality.LOW,
                reason="Live solar is required before today's export can justify charging immediately.",
            ),
            EvidenceRequirement(
                "EV charging sessions",
                EvidenceSource.IMPORTED_HISTORY if ev_actual else EvidenceSource.ESTIMATED,
                ev_actual,
                required=False,
                quality=EvidenceQuality.HIGH if ev_actual else EvidenceQuality.MEDIUM,
                reason=solar_data_status if not ev_actual else "Actual EV session history is available.",
            ),
        ]
        quality = EvidenceQuality.HIGH
        if not tariff_available or not health_ok or intervals <= 0:
            quality = EvidenceQuality.LOW
        elif daily_export_supports_current_surplus and current_bucket != "super_off_peak" and not live_solar_available:
            quality = EvidenceQuality.LOW
        elif freshness != "Fresh" or not ev_actual:
            quality = EvidenceQuality.MEDIUM
        capped = cap_confidence_for_evidence(confidence, quality)
        if daily_export_supports_current_surplus and current_bucket != "super_off_peak" and not live_solar_available:
            reason = "Live solar unavailable. Recommendation is not allowed to assume current surplus from historical export only."
        elif quality == EvidenceQuality.LOW:
            reason = "Required charging evidence is incomplete or stale."
        elif quality == EvidenceQuality.MEDIUM:
            reason = "Tariff timing is known, with some supporting evidence estimated or not live."
        else:
            reason = "Tariff timing and fresh smart-meter evidence are available."
        if capped == "Medium" and "Report age" in freshness:
            confidence_reason = f"Medium confidence because tariff data is configured but report freshness is {freshness.lower()}."
        elif quality == EvidenceQuality.HIGH:
            confidence_reason = f"{capped} confidence because tariff timing and fresh smart-meter evidence are available."
        else:
            confidence_reason = f"{capped} confidence because {reason}"
        return AdvisorEvidenceSummary(
            required_evidence=required,
            optional_evidence=optional,
            evidence_quality=quality,
            confidence=capped,
            reason=reason,
            confidence_reason=confidence_reason,
        )

    def _evidence_trail(
        self,
        *,
        bucket: str,
        next_bucket: str,
        tariff_now_rate: Optional[float],
        tariff_next_rate: Optional[float],
        per_kwh_difference: Optional[float],
        freshness: str,
        solar_status: str,
        solar_data_status: str,
        ev_session_freshness: str,
        bill_is_high: bool,
        top_priority: Optional[int],
        status: str,
        headline: str,
        benefit: Optional[FinancialImpact],
        advice_confidence: str,
    ) -> EvidenceTrail:
        items = [
            EvidenceItem(
                source="Current tariff",
                observation=f"{TARIFF_LABELS[bucket]} ({_rate_text(tariff_now_rate)})",
                impact="Sets the cost of charging if the EV is charged now.",
                confidence="High",
                freshness="Configured tariff schedule",
            ),
            EvidenceItem(
                source="Next tariff",
                observation=f"{TARIFF_LABELS[next_bucket]} ({_rate_text(tariff_next_rate)})",
                impact=(
                    f"Next-window difference is {_money_per_kwh(per_kwh_difference)}."
                    if per_kwh_difference is not None
                    else "Next-window tariff comparison is unavailable."
                ),
                confidence="High",
                freshness="Configured tariff schedule",
                financial_contribution=(
                    FinancialImpact(today=per_kwh_difference, basis="current tariff minus next recommended tariff")
                    if per_kwh_difference is not None
                    else None
                ),
            ),
            EvidenceItem(
                source="Solar data",
                observation=solar_status,
                impact=solar_data_status,
                confidence="Low" if "No solar interval" in solar_data_status else advice_confidence,
                freshness=freshness,
            ),
            EvidenceItem(
                source="EV sessions",
                observation=ev_session_freshness,
                impact="Used only as charging-history context; no live charger state is assumed.",
                confidence="High" if "Actual EV session" in ev_session_freshness else "Medium",
                freshness="Current billing period",
            ),
        ]
        if bill_is_high or top_priority is not None:
            items.append(
                EvidenceItem(
                    source="Recommendation context",
                    observation=(
                        "High billing-cost recommendation is active."
                        if bill_is_high
                        else f"Top active recommendation priority score is {top_priority}."
                    ),
                    impact="Raises the importance of avoiding avoidable charging cost.",
                    confidence=advice_confidence,
                    freshness=freshness,
                )
            )
        items.append(
            EvidenceItem(
                source="Recommendation",
                observation=headline,
                impact=f"Advisor status: {status.replace('_', ' ')}.",
                confidence=advice_confidence,
                freshness=freshness,
                financial_contribution=benefit,
            )
        )
        return EvidenceTrail(items=items)


def solar_signal(today_summary: Any) -> tuple[str, bool]:
    generated = float(getattr(today_summary, "solar_generated_kwh", 0.0) or 0.0)
    exported = float(getattr(today_summary, "solar_exported_kwh", 0.0) or 0.0)
    export_pct = float(getattr(today_summary, "solar_export_pct", 0.0) or 0.0)
    if generated <= 0:
        return "Solar unavailable", False
    if exported >= 0.5 and export_pct >= 15:
        return f"Exporting solar: {exported:.1f} kWh today", True
    return f"Solar generating: {generated:.1f} kWh today", False


def current_tariff_bucket(now: datetime) -> str:
    return context_current_tariff_bucket(now)


def next_better_window(now: datetime, current_bucket: str) -> str:
    return next_recommended_window(now, current_bucket)[1]


def next_lower_cost_window(now: datetime, current_bucket: str, rates: dict[str, float]) -> TariffWindow:
    return context_next_lower_cost_window(now, current_bucket, rates)


def lowest_cost_window(now: datetime, current_bucket: str, rates: dict[str, float]) -> TariffWindow:
    return context_lowest_cost_window(now, current_bucket, rates)


def upcoming_tariff_windows(now: datetime) -> list[tuple[datetime, str]]:
    return context_upcoming_tariff_windows(now)


def wait_minutes(now: datetime, start: Optional[datetime]) -> int:
    return context_wait_minutes(now, start)


def next_recommended_window(now: datetime, current_bucket: str) -> tuple[str, str, Optional[datetime]]:
    local_now = now.astimezone()
    if current_bucket == "super_off_peak":
        return "super_off_peak", "Now until 3:00 PM", local_now

    next_super = local_now.replace(hour=9, minute=0, second=0, microsecond=0)
    if local_now.time() >= time(15, 0):
        next_super += timedelta(days=1)
    elif local_now.time() >= time(9, 0):
        return "super_off_peak", "Now until 3:00 PM", local_now

    next_overnight = local_now.replace(hour=23, minute=0, second=0, microsecond=0)
    if local_now.time() >= time(23, 0):
        next_overnight += timedelta(days=1)

    target = min(next_super, next_overnight)
    bucket = "super_off_peak" if target == next_super else "overnight"
    label = "super off-peak" if bucket == "super_off_peak" else "overnight"
    return bucket, f"{label.title()} from {target.strftime('%-I:%M %p')}", target


def _rate_text(rate: Optional[float]) -> str:
    if rate is None:
        return "rate unavailable"
    return f"{rate * 100:.2f} c/kWh"


def _money_per_kwh(value: Optional[float]) -> str:
    if value is None:
        return "unavailable"
    return f"{value * 100:.2f} c/kWh"
