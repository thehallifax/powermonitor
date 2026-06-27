#!/usr/bin/env python3
"""Shared EV charging advisor for PowerMonitor.

The Charging Advisor combines existing tariff, solar, bill, recommendation,
decision, EV history, and freshness evidence. It does not render presentation
markup and it does not forecast future appliance behaviour.
"""

from datetime import datetime, time, timedelta
from typing import Any, Optional

from .models import ChargingAdvice, Decision, FinancialImpact

TARIFF_LABELS = {
    "overnight": "Overnight",
    "off_peak": "Off peak",
    "super_off_peak": "Super off peak",
    "peak": "Peak",
}
EV_BUCKET_LABELS = {
    "super_off_peak": "9am-3pm super off-peak / EV solar window",
    "overnight": "11pm-6am overnight",
    "off_peak": "off-peak",
    "peak": "peak",
}


class ChargingAdvisor:
    """Deterministic advisor for the question: should I charge my EV right now?"""

    name = "charging_advisor"

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
    ) -> ChargingAdvice:
        observed_at = now or datetime.now().astimezone()
        bucket = current_tariff_bucket(observed_at)
        available, freshness, confidence = self._data_is_available(today_summary, billing_summary, health)
        solar_status, has_solar_surplus = solar_signal(today_summary)
        tariff_rates = self._import_rates(settings)
        top_priority = self._top_priority(recommendations)
        bill_is_high = any(item.id == "high_cost_alert" for item in recommendations)
        per_kwh_benefit = self._per_kwh_benefit(bucket, tariff_rates)
        next_window = next_better_window(observed_at, bucket)

        evidence = {
            "current_tariff_bucket": bucket,
            "current_tariff_label": TARIFF_LABELS[bucket],
            "current_import_rate": tariff_rates.get(bucket),
            "super_off_peak_rate": tariff_rates.get("super_off_peak"),
            "solar_generated_kwh_today": self._number(today_summary, "solar_generated_kwh"),
            "solar_exported_kwh_today": self._number(today_summary, "solar_exported_kwh"),
            "solar_export_pct_today": self._number(today_summary, "solar_export_pct"),
            "solar_status": solar_status,
            "ev_charging_kwh_billing_period": self._number(billing_summary, "ev_charging_kwh"),
            "ev_charging_source": getattr(billing_summary, "ev_charging_source", None),
            "projected_bill": projected_bill,
            "bill_is_high": bill_is_high,
            "top_recommendation_priority": top_priority,
            "health_status": health.get("status"),
            "report_age_minutes": health.get("report_age_minutes"),
        }

        if not available:
            return ChargingAdvice(
                status="UNKNOWN",
                headline="Charging advice needs fresher data",
                explanation="PowerMonitor does not have fresh enough interval data to make a reliable charging call right now.",
                estimated_financial_benefit=None,
                confidence="Low",
                supporting_evidence=evidence,
                next_recommended_charging_window=next_window,
                data_freshness=freshness,
            )

        benefit = self._financial_benefit(bucket, per_kwh_benefit)
        if bucket == "super_off_peak":
            headline = "Charge now in the cheapest window"
            explanation = "The current tariff window is Super off peak, which is the lowest configured import rate."
            if has_solar_surplus:
                explanation += " Solar is also being exported today, so charging now can use more locally generated energy."
            status = "CHARGE_NOW"
        elif has_solar_surplus:
            headline = "Charge now while solar is exporting"
            explanation = "PowerMonitor can see solar export today, so charging now is supported by measured surplus solar evidence."
            status = "CHARGE_NOW"
        elif bucket == "peak":
            headline = "Avoid charging during Peak"
            explanation = "The current tariff window is Peak, the highest configured import rate, so flexible EV charging should wait."
            status = "AVOID"
        else:
            headline = "Wait for a cheaper charging window"
            explanation = "A lower-cost charging window is available later based on the configured tariff schedule."
            status = "WAIT"

        if bill_is_high and status != "CHARGE_NOW":
            explanation += " The current billing position is already flagged as high, so avoiding avoidable charging cost matters more."
        if top_priority is not None and top_priority >= 67:
            evidence["recommendation_pressure"] = "high"

        return ChargingAdvice(
            status=status,
            headline=headline,
            explanation=explanation,
            estimated_financial_benefit=benefit,
            confidence=confidence,
            supporting_evidence=evidence,
            next_recommended_charging_window=next_window,
            data_freshness=freshness,
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
    current_time = now.astimezone().time()
    if current_time >= time(23, 0) or current_time < time(6, 0):
        return "overnight"
    if time(6, 0) <= current_time < time(9, 0):
        return "off_peak"
    if time(9, 0) <= current_time < time(15, 0):
        return "super_off_peak"
    if time(15, 0) <= current_time < time(21, 0):
        return "peak"
    return "off_peak"


def next_better_window(now: datetime, current_bucket: str) -> str:
    local_now = now.astimezone()
    if current_bucket == "super_off_peak":
        return "Now until 3:00 PM"

    next_super = local_now.replace(hour=9, minute=0, second=0, microsecond=0)
    if local_now.time() >= time(15, 0):
        next_super += timedelta(days=1)
    elif local_now.time() >= time(9, 0):
        return "Now until 3:00 PM"

    next_overnight = local_now.replace(hour=23, minute=0, second=0, microsecond=0)
    if local_now.time() >= time(23, 0):
        next_overnight += timedelta(days=1)

    target = min(next_super, next_overnight)
    label = "super off-peak" if target == next_super else "overnight"
    return f"{label.title()} from {target.strftime('%-I:%M %p')}"
