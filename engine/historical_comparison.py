#!/usr/bin/env python3
"""Historical bill comparison period resolution and attribution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional


COMPARISON_TYPES = (
    "previous_billing_period",
    "same_month_last_year",
    "previous_year_to_date",
    "previous_rolling_12_months",
    "previous_equivalent_period",
    "same_period_last_year",
)

COMPARISON_LABELS = {
    "previous_billing_period": "Previous billing period",
    "same_month_last_year": "Same month last year",
    "previous_year_to_date": "Previous year to date",
    "previous_rolling_12_months": "Previous rolling 12 months",
    "previous_equivalent_period": "Previous equivalent period",
    "same_period_last_year": "Same period last year",
}

ATTRIBUTION_TOLERANCE = 0.05
EXPECTED_INTERVALS_PER_DAY = 96


@dataclass(frozen=True)
class ComparisonPeriod:
    kind: str
    label: str
    start: date
    end: date

    @property
    def day_count(self) -> int:
        return (self.end - self.start).days + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "day_count": self.day_count,
        }


def resolve_historical_comparison(
    comparison_type: str,
    primary_period: Any,
    *,
    today: date,
    settings: Any,
) -> dict[str, Any]:
    comparison_type = comparison_type if comparison_type in COMPARISON_TYPES else "previous_billing_period"
    primary = _primary_for_type(comparison_type, primary_period, today, settings)
    comparison = _comparison_for_type(comparison_type, primary, settings)
    warnings = []
    if primary.start <= comparison.end and comparison.start <= primary.end:
        warnings.append("Comparison periods overlap.")
    if primary.end >= today and comparison.day_count != primary.day_count:
        warnings.append("Current period is partial and is being compared with a different-length historical period.")
    elif primary.end >= today:
        warnings.append("Current period may be partial; compare average daily values as well as totals.")
    if primary.day_count != comparison.day_count:
        warnings.append(f"Period lengths differ: {primary.day_count} days vs {comparison.day_count} days.")
    return {
        "comparison_type": comparison_type,
        "label": COMPARISON_LABELS[comparison_type],
        "primary_period": primary.as_dict(),
        "comparison_period": comparison.as_dict(),
        "local_day_count": primary.day_count,
        "comparison_day_count": comparison.day_count,
        "completeness": "pending_metrics",
        "overlap_warnings": [item for item in warnings if "overlap" in item.lower()],
        "partial_period_warning": next((item for item in warnings if "partial" in item.lower()), None),
        "warnings": warnings,
        "data_coverage": {},
        "tariff_coverage": {},
    }


def build_historical_comparison(
    resolved: dict[str, Any],
    primary_summary: Any,
    comparison_summary: Any,
) -> dict[str, Any]:
    primary_metrics = _metrics(primary_summary)
    comparison_metrics = _metrics(comparison_summary)
    changes = {
        key: _change(primary_metrics[key], comparison_metrics[key])
        for key in primary_metrics
    }
    tariff_payload = {
        "primary_period": _tariff_coverage(primary_summary),
        "comparison_period": _tariff_coverage(comparison_summary),
    }
    attribution = _attribution(primary_summary, comparison_summary)
    confidence = _confidence(primary_summary, comparison_summary, resolved, tariff_payload)
    warnings = list(resolved.get("warnings") or [])
    warnings.extend(confidence["warnings"])
    outcome = _outcome_sentence(primary_summary, comparison_summary, changes)
    result = {
        **resolved,
        "completeness": confidence["completeness"],
        "data_coverage": {
            "primary_period": _data_coverage(primary_summary),
            "comparison_period": _data_coverage(comparison_summary),
        },
        "tariff_coverage": tariff_payload,
        "tariffs_used": tariff_payload,
        "metrics": {
            "primary_period": primary_metrics,
            "comparison_period": comparison_metrics,
        },
        "changes": changes,
        "attribution": attribution,
        "confidence": confidence,
        "warnings": warnings,
        "outcome": outcome,
    }
    return result


def _primary_for_type(comparison_type: str, primary_period: Any, today: date, settings: Any) -> ComparisonPeriod:
    if comparison_type == "previous_billing_period":
        if _value(primary_period, "kind") != "current_billing_period":
            return ComparisonPeriod(
                str(_value(primary_period, "kind", "custom")),
                str(_value(primary_period, "label", "Primary period")),
                _value(primary_period, "start"),
                _value(primary_period, "end"),
            )
        period_start = _value(settings, "current_billing_period_start") or _value(primary_period, "start")
        period_end = _value(primary_period, "end")
        return ComparisonPeriod("current_billing_period", "Current billing period", period_start, period_end)
    if comparison_type == "same_month_last_year":
        return ComparisonPeriod("this_month", "Current month", date(today.year, today.month, 1), today)
    if comparison_type == "previous_year_to_date":
        return ComparisonPeriod("year_to_date", "Year to date", date(today.year, 1, 1), today)
    if comparison_type == "previous_rolling_12_months":
        return ComparisonPeriod("rolling_12_months", "Rolling 12 months", today - timedelta(days=364), today)
    return ComparisonPeriod(
        str(_value(primary_period, "kind", "custom")),
        str(_value(primary_period, "label", "Primary period")),
        _value(primary_period, "start"),
        _value(primary_period, "end"),
    )


def _comparison_for_type(comparison_type: str, primary: ComparisonPeriod, settings: Any) -> ComparisonPeriod:
    if comparison_type == "previous_billing_period":
        start = _value(settings, "previous_billing_period_start")
        end = _value(settings, "previous_billing_period_end")
        if primary.kind == "current_billing_period" and start and end:
            return ComparisonPeriod("previous_billing_period", "Previous billing period", start, end)
        end = primary.start - timedelta(days=1)
        return ComparisonPeriod("previous_billing_period", "Previous billing period", end - timedelta(days=primary.day_count - 1), end)
    if comparison_type == "same_month_last_year":
        return ComparisonPeriod("same_month_last_year", "Same month last year", _shift_year(primary.start, -1), _shift_year(primary.end, -1))
    if comparison_type == "previous_year_to_date":
        return ComparisonPeriod("previous_year_to_date", "Previous year to date", date(primary.start.year - 1, 1, 1), _shift_year(primary.end, -1))
    if comparison_type == "previous_rolling_12_months":
        end = primary.start - timedelta(days=1)
        return ComparisonPeriod("previous_rolling_12_months", "Previous rolling 12 months", end - timedelta(days=primary.day_count - 1), end)
    if comparison_type == "same_period_last_year":
        return ComparisonPeriod("same_period_last_year", "Same period last year", _shift_year(primary.start, -1), _shift_year(primary.end, -1))
    end = primary.start - timedelta(days=1)
    return ComparisonPeriod("previous_equivalent_period", "Previous equivalent period", end - timedelta(days=primary.day_count - 1), end)


def _metrics(summary: Any) -> dict[str, float]:
    buckets = _value(summary, "buckets", {}) or {}
    return {
        "net_estimated_cost": _number(summary, "net_cost"),
        "import_cost": _number(summary, "import_cost"),
        "supply_charge": _number(summary, "supply_cost"),
        "export_credit": _number(summary, "export_credit"),
        "grid_import_kwh": _number(summary, "grid_import_kwh"),
        "solar_generation_kwh": _number(summary, "solar_generated_kwh"),
        "solar_used_directly_kwh": _number(summary, "solar_used_kwh"),
        "solar_export_kwh": _number(summary, "solar_exported_kwh"),
        "ev_charging_energy_kwh": _number(summary, "ev_charging_kwh"),
        "ev_charging_cost": _number(summary, "ev_charging_cost"),
        "peak_usage_kwh": float((buckets.get("peak") or {}).get("kwh") or 0.0),
        "off_peak_usage_kwh": float((buckets.get("off_peak") or {}).get("kwh") or 0.0),
        "super_off_peak_usage_kwh": float((buckets.get("super_off_peak") or {}).get("kwh") or 0.0),
        "average_daily_cost": _number(summary, "average_daily_net_cost"),
        "average_daily_import_kwh": _number(summary, "average_daily_grid_import_kwh"),
        "average_daily_solar_kwh": _number(summary, "average_daily_solar_generation_kwh"),
        "covered_days": float(_value(summary, "reading_days", 0) or 0),
    }


def _change(primary: float, comparison: float) -> dict[str, Optional[float]]:
    absolute = primary - comparison
    percent = None if abs(comparison) < 1e-9 else absolute / comparison * 100
    return {"primary": primary, "comparison": comparison, "absolute": absolute, "percent": percent}


def _attribution(primary: Any, comparison: Any) -> dict[str, Any]:
    total = _number(primary, "net_cost") - _number(comparison, "net_cost")
    supply = _number(primary, "supply_cost") - _number(comparison, "supply_cost")
    export = -(_number(primary, "export_credit") - _number(comparison, "export_credit"))
    ev = _number(primary, "ev_charging_cost") - _number(comparison, "ev_charging_cost")
    p_import = _number(primary, "import_cost")
    c_import = _number(comparison, "import_cost")
    p_kwh = _number(primary, "grid_import_kwh")
    c_kwh = _number(comparison, "grid_import_kwh")
    p_rate = p_import / p_kwh if p_kwh else 0.0
    c_rate = c_import / c_kwh if c_kwh else 0.0
    tariff = p_kwh * (p_rate - c_rate)
    period_length = 0.0
    day_delta = int(_value(primary, "supply_days", 0) or 0) - int(_value(comparison, "supply_days", 0) or 0)
    if day_delta:
        period_length = _number(comparison, "average_daily_net_cost") * day_delta
    known = supply + export + ev + tariff + period_length
    grid_usage = total - known
    components = [
        _component("grid_usage_change", "Grid usage and household load changed", grid_usage, "Residual import effect after EV, tariff and period-length effects; solar self-consumption changes are captured here."),
        _component("tariff_rate_change", "Tariff/rate change", tariff, "Difference in average import rate applied to primary-period import volume."),
        _component("daily_supply_charge", "Daily supply charge", supply, "Change in fixed daily supply charges across the compared periods."),
        _component("ev_charging_change", "EV charging change", ev, "Change in EV charging cost from actual sessions or the existing EV estimate."),
        _component("solar_generation_self_consumption", "Solar generation/self-consumption change", 0.0, "Solar effects reduce import or change export credits; they are not double counted separately."),
        _component("export_credit_change", "Export credits changed", export, "Net bill impact of export-credit changes. More export credit lowers the bill."),
        _component("period_length_difference", "Period length difference", period_length, "Estimated effect of comparing periods with different local day counts."),
        _component("unresolved_data_effect", "Unresolved data effect", 0.0, "No additional unresolved-data adjustment was required for reconciliation."),
    ]
    reconciled = sum(item["amount"] for item in components)
    return {
        "method": "Net bill delta = supply delta + export-credit bill impact + EV cost delta + average-rate tariff effect + period-length effect + residual grid/solar usage effect. Solar is named but not double counted separately.",
        "total_difference": total,
        "components": sorted(components, key=lambda item: abs(item["amount"]), reverse=True),
        "reconciled_total": reconciled,
        "reconciliation_difference": total - reconciled,
        "tolerance": ATTRIBUTION_TOLERANCE,
        "reconciles": abs(total - reconciled) <= ATTRIBUTION_TOLERANCE,
    }


def _component(code: str, label: str, amount: float, explanation: str) -> dict[str, Any]:
    return {"code": code, "label": label, "amount": amount, "explanation": explanation}


def _data_coverage(summary: Any) -> dict[str, Any]:
    days = int(_value(summary, "supply_days", 0) or 0)
    intervals = int(_value(summary, "intervals", 0) or 0)
    expected = days * EXPECTED_INTERVALS_PER_DAY
    return {
        "covered_days": int(_value(summary, "reading_days", 0) or 0),
        "local_day_count": days,
        "intervals": intervals,
        "expected_intervals": expected,
        "interval_coverage_pct": intervals / expected * 100 if expected else 0.0,
        "solar_coverage": "available" if _number(summary, "solar_generated_kwh") > 0 else "missing_or_zero",
        "ev_session_coverage": str(_value(summary, "ev_charging_source", "estimated") or "estimated"),
    }


def _tariff_coverage(summary: Any) -> dict[str, Any]:
    resolution = _value(summary, "tariff_resolution", {}) or {}
    periods = list(resolution.get("periods_used") or [])
    return {
        "tariffs_used": periods,
        "tariff_changes": len(periods) > 1,
        "unresolved_tariff_gaps": int(resolution.get("unresolved_intervals") or 0),
        "fallback_tariff_usage": int(resolution.get("fallback_intervals") or 0),
        "confidence": resolution.get("confidence") or "unknown",
        "warnings": list(resolution.get("warnings") or []),
    }


def _confidence(primary: Any, comparison: Any, resolved: dict[str, Any], tariffs: dict[str, Any]) -> dict[str, Any]:
    warnings = []
    score = 100
    for label, summary in (("primary", primary), ("comparison", comparison)):
        coverage = _data_coverage(summary)
        if coverage["interval_coverage_pct"] < 80:
            score -= 25
            warnings.append(f"{label} interval coverage is below 80%.")
        if coverage["covered_days"] < coverage["local_day_count"]:
            score -= 15
            warnings.append(f"{label} has incomplete day coverage.")
        if coverage["ev_session_coverage"] == "estimated":
            score -= 5
            warnings.append(f"{label} EV charging uses estimated data.")
        if coverage["solar_coverage"] == "missing_or_zero":
            score -= 5
            warnings.append(f"{label} solar data is missing or zero.")
    for label in ("primary_period", "comparison_period"):
        tariff = tariffs[label]
        if tariff["unresolved_tariff_gaps"]:
            score -= 20
            warnings.append(f"{label} has unresolved tariff gaps.")
        if tariff["fallback_tariff_usage"]:
            score -= 10
            warnings.append(f"{label} used fallback tariffs.")
    if resolved.get("primary_period", {}).get("day_count") != resolved.get("comparison_period", {}).get("day_count"):
        score -= 10
        warnings.append("Compared periods have different lengths.")
    if resolved.get("partial_period_warning"):
        score -= 5
    if score >= 90:
        level = "Very High"
    elif score >= 75:
        level = "High"
    elif score >= 50:
        level = "Medium"
    elif score >= 25:
        level = "Low"
    else:
        level = "Insufficient data"
    return {
        "level": level,
        "score": max(score, 0),
        "warnings": warnings,
        "completeness": "complete" if score >= 75 else "partial" if score >= 25 else "insufficient",
    }


def _outcome_sentence(primary: Any, comparison: Any, changes: dict[str, Any]) -> str:
    delta = changes["net_estimated_cost"]["absolute"]
    if abs(delta) < 0.01:
        return "This period is about the same cost as the comparison period."
    direction = "higher" if delta > 0 else "lower"
    avg = changes["average_daily_cost"]["absolute"]
    avg_text = f" Average daily cost is {'up' if avg > 0 else 'down'} {abs(avg):.2f} dollars."
    return f"This period is ${abs(delta):.0f} {direction} than the comparison period.{avg_text}"


def _shift_year(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _number(source: Any, name: str, default: float = 0.0) -> float:
    return float(_value(source, name, default) or 0.0)
