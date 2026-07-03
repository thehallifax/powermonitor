"""Long-term household trends and personal records."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class HouseholdTrend:
    trend_id: str
    label: str
    current_value: str
    previous_value: str
    percentage_change: Optional[float]
    direction: str
    confidence: str
    explanation: str
    topic: str
    usefulness: float = field(default=0.0, compare=False)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("usefulness", None)
        return data


@dataclass(frozen=True)
class HouseholdRecord:
    record_id: str
    label: str
    value: str
    date: str
    previous_record: Optional[str]
    confidence: str
    explanation: str
    topic: str
    usefulness: float = field(default=0.0, compare=False)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("usefulness", None)
        return data


@dataclass(frozen=True)
class HouseholdTrendsReport:
    trends: list[HouseholdTrend]
    records: list[HouseholdRecord]

    def as_dict(self) -> dict[str, Any]:
        return {
            "trends": [trend.as_dict() for trend in self.trends],
            "records": [record.as_dict() for record in self.records],
        }


class TrendsEngine:
    """Convert historical household data into context, not recommendations."""

    name = "trends_engine"

    def build(
        self,
        *,
        current_summary: Any,
        previous_summary: Optional[Any],
        current_trends: Iterable[Any],
        previous_trends: Optional[Iterable[Any]] = None,
        comparison_label: str = "previous period",
        insights: Optional[Iterable[Any]] = None,
        recommendations: Optional[Iterable[Any]] = None,
    ) -> HouseholdTrendsReport:
        current_days = max(_number(current_summary, "reading_days"), 0.0)
        previous_days = max(_number(previous_summary, "reading_days"), 0.0) if previous_summary else 0.0
        if current_days < 3 or previous_days < 3:
            return HouseholdTrendsReport([], self._records(list(current_trends or []), list(previous_trends or []), current_days))

        blocked_topics = _blocked_topics(insights or [], recommendations or [])
        trend_candidates = self._summary_trends(current_summary, previous_summary, comparison_label, int(current_days + previous_days))
        trends = []
        seen: set[str] = set()
        for trend in sorted(trend_candidates, key=lambda item: item.usefulness, reverse=True):
            if trend.topic in blocked_topics or trend.topic in seen:
                continue
            if trend.percentage_change is None or abs(trend.percentage_change) < 8:
                continue
            trends.append(trend)
            seen.add(trend.topic)

        records = [
            record for record in self._records(list(current_trends or []), list(previous_trends or []), current_days)
            if record.topic not in blocked_topics
        ]
        return HouseholdTrendsReport(trends=trends, records=records)

    def _summary_trends(
        self,
        current: Any,
        previous: Any,
        comparison_label: str,
        sample_count: int,
    ) -> list[HouseholdTrend]:
        specs = [
            ("average_daily_cost", "Average daily cost", _number(current, "average_daily_net_cost"), _number(previous, "average_daily_net_cost"), "money", "cost"),
            ("grid_import", "Grid import", _per_day(current, "grid_import_kwh"), _per_day(previous, "grid_import_kwh"), "kwh", "grid"),
            ("solar_generation", "Solar generation", _per_day(current, "solar_generated_kwh"), _per_day(previous, "solar_generated_kwh"), "kwh", "solar_generation"),
            ("self_consumption", "Solar self-consumption", _number(current, "self_consumption_pct"), _number(previous, "self_consumption_pct"), "percent", "solar_flow"),
            ("export", "Solar export", _per_day(current, "solar_exported_kwh"), _per_day(previous, "solar_exported_kwh"), "kwh", "solar_export"),
            ("peak_usage", "Peak usage", _bucket_per_day(current, "peak", "kwh"), _bucket_per_day(previous, "peak", "kwh"), "kwh", "peak_usage"),
            ("overnight_usage", "Overnight usage", _bucket_per_day(current, "overnight", "kwh"), _bucket_per_day(previous, "overnight", "kwh"), "kwh", "overnight_usage"),
            ("ev_energy", "EV charging energy", _per_day(current, "ev_charging_kwh"), _per_day(previous, "ev_charging_kwh"), "kwh", "ev_charging"),
            ("ev_cost", "EV charging cost", _per_day(current, "ev_charging_cost"), _per_day(previous, "ev_charging_cost"), "money", "ev_charging"),
        ]
        trends = []
        for trend_id, label, current_value, previous_value, unit, topic in specs:
            if current_value is None or previous_value is None or previous_value <= 0:
                continue
            change = (current_value - previous_value) / previous_value * 100
            direction = "up" if change > 2 else "down" if change < -2 else "flat"
            trends.append(
                HouseholdTrend(
                    trend_id=trend_id,
                    label=label,
                    current_value=_format_value(current_value, unit),
                    previous_value=_format_value(previous_value, unit),
                    percentage_change=change,
                    direction=direction,
                    confidence=_confidence(sample_count),
                    explanation=_trend_explanation(label, direction, comparison_label),
                    topic=topic,
                    usefulness=abs(change) + (8 if topic in {"cost", "peak_usage", "solar_generation"} else 0),
                )
            )
        return trends

    def _records(self, current_trends: list[Any], previous_trends: list[Any], sample_days: float) -> list[HouseholdRecord]:
        current = [trend for trend in current_trends if _number(trend, "net_cost") or _number(trend, "solar_generated_kwh") or _number(trend, "grid_import_kwh")]
        if len(current) < 3:
            return []
        specs = [
            ("highest_solar_generation_day", "Highest solar generation day", "solar_generated_kwh", max, "kwh", "solar_generation"),
            ("lowest_cost_day", "Lowest-cost day", "net_cost", min, "money", "cost"),
            ("highest_export_day", "Highest export day", "solar_exported_kwh", max, "kwh", "solar_export"),
            ("best_self_consumption_day", "Best self-consumption day", "_self_consumption_pct", max, "percent", "solar_flow"),
            ("most_ev_charging_day", "Most EV charging in one day", "ev_charging_kwh", max, "kwh", "ev_charging"),
            ("highest_home_energy_use_day", "Highest home energy use", "grid_import_kwh", max, "kwh", "grid"),
        ]
        records: list[HouseholdRecord] = []
        for record_id, label, field_name, selector, unit, topic in specs:
            values = [(trend, _record_value(trend, field_name)) for trend in current]
            values = [(trend, value) for trend, value in values if value is not None and value > 0]
            if len(values) < 3 and record_id != "most_ev_charging_day":
                continue
            if not values:
                continue
            trend, value = selector(values, key=lambda item: item[1])
            previous_values = [_record_value(item, field_name) for item in previous_trends or []]
            previous_values = [value for value in previous_values if value is not None and value > 0]
            previous_record = None
            novelty = 0.0
            if previous_values:
                previous_value = selector(previous_values)
                previous_record = _format_value(previous_value, unit)
                if previous_value:
                    novelty = abs(value - previous_value) / previous_value * 100
            records.append(
                HouseholdRecord(
                    record_id=record_id,
                    label=label,
                    value=_format_value(value, unit),
                    date=_day_label(trend),
                    previous_record=previous_record,
                    confidence=_confidence(int(sample_days)),
                    explanation=_record_explanation(label, bool(previous_values)),
                    topic=topic,
                    usefulness=novelty + (18 if record_id in {"highest_solar_generation_day", "lowest_cost_day", "highest_export_day"} else 0),
                )
            )
        return sorted(records, key=lambda item: item.usefulness, reverse=True)


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _number(item: Any, name: str) -> float:
    try:
        return float(_value(item, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _per_day(summary: Any, field_name: str) -> Optional[float]:
    days = max(_number(summary, "reading_days"), 0.0)
    if days <= 0:
        return None
    return _number(summary, field_name) / days


def _bucket_per_day(summary: Any, bucket: str, field_name: str) -> Optional[float]:
    days = max(_number(summary, "reading_days"), 0.0)
    if days <= 0:
        return None
    buckets = _value(summary, "buckets", {}) or {}
    return _number((buckets.get(bucket) or {}), field_name) / days


def _record_value(trend: Any, field_name: str) -> Optional[float]:
    if field_name == "_self_consumption_pct":
        generated = _number(trend, "solar_generated_kwh")
        if generated <= 0:
            return None
        exported = _number(trend, "solar_exported_kwh")
        return max(0.0, min(100.0, (generated - exported) / generated * 100))
    return _number(trend, field_name)


def _format_value(value: float, unit: str) -> str:
    if unit == "money":
        return f"${value:,.2f}"
    if unit == "percent":
        return f"{value:.0f}%"
    return f"{value:,.1f} kWh"


def _confidence(sample_count: int) -> str:
    if sample_count >= 28:
        return "High"
    if sample_count >= 10:
        return "Medium"
    return "Low"


def _trend_explanation(label: str, direction: str, comparison_label: str) -> str:
    comparison = f"compared with {comparison_label}"
    if direction == "flat":
        return f"{label} is broadly steady {comparison}."
    if label == "Peak usage":
        return f"Your household used {'more' if direction == 'up' else 'less'} electricity during expensive tariff periods {comparison}."
    if label == "Average daily cost":
        return f"Daily cost is {'higher' if direction == 'up' else 'lower'} {comparison} based on the bill totals PowerMonitor can verify."
    if label == "Solar self-consumption":
        return f"The share of solar used at home is {'higher' if direction == 'up' else 'lower'} {comparison}."
    return f"{label} is {'higher' if direction == 'up' else 'lower'} {comparison}."


def _record_explanation(label: str, has_previous: bool) -> str:
    subject = label.lower()
    if subject.startswith("best ") or subject.startswith("lowest "):
        base = f"This is the {subject} PowerMonitor found in the current period"
    elif subject.startswith("highest ") or subject.startswith("most "):
        base = f"This is the {subject} PowerMonitor found in the current period"
    else:
        base = f"This record stood out in the current period"
    if has_previous:
        return f"{base}, compared with the previous period record."
    return f"{base}."


def _day_label(item: Any) -> str:
    day = _value(item, "day")
    if hasattr(day, "strftime"):
        return day.strftime("%-d %b")
    return str(day or "Unknown date")


def _blocked_topics(insights: Iterable[Any], recommendations: Iterable[Any]) -> set[str]:
    topics = {
        str(_value(insight, "topic", ""))
        for insight in insights
        if _value(insight, "topic", "")
    }
    text = _normalise(" ".join(str(_value(item, "headline", "") or _value(item, "title", "")) for item in recommendations))
    if "peak" in text:
        topics.add("peak_usage")
    if "solar" in text:
        topics.add("solar_generation")
    if "ev" in text or "charging" in text:
        topics.add("ev_charging")
    return topics


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
