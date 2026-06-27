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
        next_bucket, next_window, next_window_start = next_recommended_window(observed_at, bucket)
        tariff_now_rate = tariff_rates.get(bucket)
        tariff_next_rate = tariff_rates.get(next_bucket)
        per_kwh_difference = self._per_kwh_difference(bucket, next_bucket, tariff_rates)
        per_kwh_benefit = self._per_kwh_benefit(bucket, tariff_rates)
        charger_data_status, ev_session_freshness = self._charger_data_status(billing_summary)
        solar_data_status = self._solar_data_status(today_summary, freshness)
        confidence_reason = self._confidence_reason(confidence, freshness, charger_data_status, solar_data_status)

        evidence = {
            "current_tariff_bucket": bucket,
            "current_tariff_label": TARIFF_LABELS[bucket],
            "current_import_rate": tariff_now_rate,
            "super_off_peak_rate": tariff_rates.get("super_off_peak"),
            "next_tariff_bucket": next_bucket,
            "next_tariff_label": TARIFF_LABELS[next_bucket],
            "next_import_rate": tariff_next_rate,
            "per_kwh_difference": per_kwh_difference,
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
                explanation=(
                    "PowerMonitor does not have fresh enough interval data to make a reliable charging call. "
                    f"The tariff schedule is still known, but solar, billing, and EV evidence are {freshness.lower()}."
                ),
                estimated_financial_benefit=None,
                confidence="Low",
                supporting_evidence=evidence,
                next_recommended_charging_window=next_window,
                data_freshness=freshness,
                current_window=EV_BUCKET_LABELS[bucket],
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
            headline = "Charge now based on today's solar export"
            explanation = (
                "Advice is solar/export-based: today's measured solar export summary shows exported energy. "
                "This is daily export evidence, not live instantaneous charger or solar telemetry."
            )
            status = "CHARGE_NOW"
        elif bucket == "peak":
            headline = "Avoid charging during Peak"
            explanation = (
                f"Advice is tariff-based: the current Peak rate is ${tariff_now_rate:.4f}/kWh. "
                f"The next recommended window is {next_window} at ${tariff_next_rate:.4f}/kWh."
            )
            status = "AVOID"
        else:
            headline = "Wait for a cheaper charging window"
            explanation = (
                f"Advice is tariff-based: the current {TARIFF_LABELS[bucket]} rate is ${tariff_now_rate:.4f}/kWh. "
                f"The next recommended lower-cost window is {next_window} at ${tariff_next_rate:.4f}/kWh."
            )
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
            current_window=EV_BUCKET_LABELS[bucket],
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
    return next_recommended_window(now, current_bucket)[1]


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
