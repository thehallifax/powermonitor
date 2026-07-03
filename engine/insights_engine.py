"""Household observation engine for homeowner-facing insights."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class HouseholdInsight:
    """A plain-language observation that does not require action."""

    insight_id: str
    headline: str
    explanation: str
    comparison_period: str
    confidence: str
    supporting_values: dict[str, str]
    topic: str
    usefulness: float = field(default=0.0, compare=False)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("usefulness", None)
        return data


class HouseholdInsightsEngine:
    """Generate interesting observations from existing household history."""

    name = "household_insights_engine"

    def build(
        self,
        *,
        trends: Iterable[Any],
        current_summary: Any,
        billing_summary: Any,
        comparison: Optional[dict[str, Any]] = None,
        projected_bill: Optional[float] = None,
        recommendations: Optional[Iterable[Any]] = None,
        limit: int = 3,
    ) -> list[HouseholdInsight]:
        trend_list = sorted(list(trends or []), key=lambda item: _value(item, "day"))
        candidates: list[HouseholdInsight] = []
        candidates.extend(self._daily_solar_insights(trend_list))
        candidates.extend(self._cost_streak_insights(trend_list))
        candidates.extend(self._period_extreme_insights(trend_list))
        candidates.extend(self._ev_tariff_insights(billing_summary))
        candidates.extend(self._bill_projection_insights(current_summary, comparison, projected_bill))
        candidates.extend(self._peak_usage_insights(comparison))

        recommendation_text = _recommendation_text(recommendations or [])
        ranked = sorted(candidates, key=lambda insight: insight.usefulness, reverse=True)
        selected: list[HouseholdInsight] = []
        seen_topics: set[str] = set()
        for insight in ranked:
            if insight.topic in seen_topics:
                continue
            if _duplicates_recommendation(insight, recommendation_text):
                continue
            selected.append(insight)
            seen_topics.add(insight.topic)
            if len(selected) >= limit:
                break
        return selected

    def _daily_solar_insights(self, trends: list[Any]) -> list[HouseholdInsight]:
        if len(trends) < 5:
            return []
        latest = trends[-1]
        baseline = [trend for trend in trends[-8:-1] if _number(trend, "solar_generated_kwh") > 0]
        if len(baseline) < 3 or _number(latest, "solar_generated_kwh") <= 1.0:
            return []

        insights: list[HouseholdInsight] = []
        latest_solar = _number(latest, "solar_generated_kwh")
        avg_solar = _average(_number(trend, "solar_generated_kwh") for trend in baseline)
        if avg_solar > 0:
            pct = _pct_change(latest_solar, avg_solar)
            if abs(pct) >= 25 and abs(latest_solar - avg_solar) >= 2.0:
                direction = "more" if pct > 0 else "less"
                insights.append(
                    HouseholdInsight(
                        insight_id="solar_generation_recent_change",
                        headline=f"Solar generation was {abs(pct):.0f}% {direction} than usual.",
                        explanation=(
                            f"PowerMonitor compared the latest solar day with the previous {len(baseline)} solar days. "
                            f"That stands out because generation changed by {_kwh(abs(latest_solar - avg_solar))}."
                        ),
                        comparison_period=f"Latest day compared with previous {len(baseline)} solar days",
                        confidence=_confidence(len(baseline) + 1),
                        supporting_values={
                            "Latest solar": _kwh(latest_solar),
                            "Recent average": _kwh(avg_solar),
                            "Difference": f"{pct:+.0f}%",
                        },
                        topic="solar_generation",
                        usefulness=min(abs(pct), 80) + 18,
                    )
                )

        latest_export_pct = _export_pct(latest)
        baseline_export = [_export_pct(trend) for trend in baseline if _number(trend, "solar_generated_kwh") > 1.0]
        avg_export_pct = _average(baseline_export)
        if avg_export_pct is not None and latest_export_pct is not None:
            delta = latest_export_pct - avg_export_pct
            if abs(delta) >= 12:
                direction = "higher" if delta > 0 else "lower"
                insights.append(
                    HouseholdInsight(
                        insight_id="solar_export_share_recent_change",
                        headline=f"Solar exports were {abs(delta):.0f} percentage points {direction} than usual.",
                        explanation=(
                            "The share of solar sent to the grid changed noticeably compared with recent solar days. "
                            "That can reveal a shift in when the household is using electricity."
                        ),
                        comparison_period=f"Latest day compared with previous {len(baseline_export)} solar days",
                        confidence=_confidence(len(baseline_export) + 1),
                        supporting_values={
                            "Latest export share": f"{latest_export_pct:.0f}%",
                            "Recent average": f"{avg_export_pct:.0f}%",
                            "Difference": f"{delta:+.0f} pp",
                        },
                        topic="solar_flow",
                        usefulness=abs(delta) * 2 + 12,
                    )
                )

        latest_self = 100.0 - latest_export_pct if latest_export_pct is not None else None
        baseline_self_values = [100.0 - value for value in baseline_export]
        avg_self = _average(baseline_self_values)
        if latest_self is not None and avg_self is not None:
            delta = latest_self - avg_self
            if abs(delta) >= 12:
                direction = "higher" if delta > 0 else "lower"
                insights.append(
                    HouseholdInsight(
                        insight_id="solar_self_consumption_recent_change",
                        headline=f"Solar self-consumption was {abs(delta):.0f} percentage points {direction} than usual.",
                        explanation=(
                            "PowerMonitor noticed a meaningful change in how much rooftop solar stayed in the home. "
                            "That is interesting because self-consumption often changes the bill more than export volume alone."
                        ),
                        comparison_period=f"Latest day compared with previous {len(baseline_self_values)} solar days",
                        confidence=_confidence(len(baseline_self_values) + 1),
                        supporting_values={
                            "Latest self-consumption": f"{latest_self:.0f}%",
                            "Recent average": f"{avg_self:.0f}%",
                            "Difference": f"{delta:+.0f} pp",
                        },
                        topic="solar_flow",
                        usefulness=abs(delta) * 2 + 10,
                    )
                )
        return insights

    def _cost_streak_insights(self, trends: list[Any]) -> list[HouseholdInsight]:
        if len(trends) < 7:
            return []
        recent = trends[-3:]
        baseline = trends[:-3]
        if len(baseline) < 4:
            return []
        avg_cost = _average(_number(trend, "net_cost") for trend in baseline)
        if not avg_cost or avg_cost <= 0:
            return []
        if all(_number(trend, "net_cost") <= avg_cost * 0.9 for trend in recent):
            recent_avg = _average(_number(trend, "net_cost") for trend in recent) or 0.0
            saving = avg_cost - recent_avg
            if saving >= 0.75:
                return [
                    HouseholdInsight(
                        insight_id="consecutive_days_below_average_cost",
                        headline=f"The last 3 days were about {_money(saving)} per day below your recent average.",
                        explanation=(
                            "Three days in a row came in lower than the earlier billing-period pattern. "
                            "That is interesting because it suggests a short run of lower-cost behaviour, not just a one-day dip."
                        ),
                        comparison_period="Last 3 days compared with earlier days this billing period",
                        confidence=_confidence(len(trends)),
                        supporting_values={
                            "Last 3 day average": _money(recent_avg),
                            "Earlier average": _money(avg_cost),
                            "Difference": _money(saving),
                        },
                        topic="cost_streak",
                        usefulness=55 + min(saving * 4, 25),
                    )
                ]
        return []

    def _period_extreme_insights(self, trends: list[Any]) -> list[HouseholdInsight]:
        if len(trends) < 6:
            return []
        insights: list[HouseholdInsight] = []
        avg_cost = _average(_number(trend, "net_cost") for trend in trends)
        lowest = min(trends, key=lambda trend: _number(trend, "net_cost"))
        lowest_cost = _number(lowest, "net_cost")
        if avg_cost and avg_cost > 0 and lowest_cost <= avg_cost * 0.75 and avg_cost - lowest_cost >= 1.0:
            insights.append(
                HouseholdInsight(
                    insight_id="lowest_cost_day_billing_period",
                    headline=f"{_day_label(lowest)} was your lowest-cost day this billing period.",
                    explanation=(
                        f"That day was {_money(avg_cost - lowest_cost)} below the daily average. "
                        "It is useful because it shows what a lower-cost household day looked like in this period."
                    ),
                    comparison_period="Current billing period daily average",
                    confidence=_confidence(len(trends)),
                    supporting_values={
                        "Lowest-cost day": _money(lowest_cost),
                        "Daily average": _money(avg_cost),
                        "Difference": _money(avg_cost - lowest_cost),
                    },
                    topic="lowest_cost_day",
                    usefulness=48 + min((avg_cost - lowest_cost) * 4, 20),
                )
            )

        solar_days = [trend for trend in trends if _number(trend, "solar_generated_kwh") > 0]
        if len(solar_days) >= 5:
            avg_solar = _average(_number(trend, "solar_generated_kwh") for trend in solar_days)
            highest = max(solar_days, key=lambda trend: _number(trend, "solar_generated_kwh"))
            highest_solar = _number(highest, "solar_generated_kwh")
            if avg_solar and highest_solar >= avg_solar * 1.25 and highest_solar - avg_solar >= 2.0:
                insights.append(
                    HouseholdInsight(
                        insight_id="highest_solar_day_billing_period",
                        headline=f"{_day_label(highest)} was your strongest solar day this billing period.",
                        explanation=(
                            f"Solar generation was {_kwh(highest_solar - avg_solar)} above the billing-period daily average. "
                            "That is interesting because it marks the best recent solar production day PowerMonitor can verify."
                        ),
                        comparison_period="Current billing period solar days",
                        confidence=_confidence(len(solar_days)),
                        supporting_values={
                            "Highest solar day": _kwh(highest_solar),
                            "Solar day average": _kwh(avg_solar),
                            "Difference": _kwh(highest_solar - avg_solar),
                        },
                        topic="solar_generation",
                        usefulness=46 + min(_pct_change(highest_solar, avg_solar), 30),
                    )
                )
        return insights

    def _ev_tariff_insights(self, summary: Any) -> list[HouseholdInsight]:
        breakdown = _value(summary, "actual_ev_tariff_breakdown") or {}
        total = sum(_number((breakdown.get(bucket) or {}), "kwh") for bucket in ("super_off_peak", "overnight", "off_peak", "peak"))
        if total < 5.0:
            return []
        cheap = sum(_number((breakdown.get(bucket) or {}), "kwh") for bucket in ("super_off_peak", "overnight"))
        share = cheap / total * 100 if total else 0.0
        if share < 90:
            return []
        label = "All" if share >= 99.5 else f"{share:.0f}% of"
        return [
            HouseholdInsight(
                insight_id="ev_charging_cheaper_tariff_share",
                headline=f"{label} EV charging happened in cheaper tariff periods this billing period.",
                explanation=(
                    "PowerMonitor checked the tariff breakdown for measured EV charging sessions. "
                    "This stands out because charging avoided the most expensive periods."
                ),
                comparison_period="Current billing period EV session tariff breakdown",
                confidence="High",
                supporting_values={
                    "Cheaper-window charging": _kwh(cheap),
                    "Total EV charging": _kwh(total),
                    "Share": f"{share:.0f}%",
                },
                topic="ev_charging",
                usefulness=72 + min(share - 90, 10),
            )
        ]

    def _bill_projection_insights(
        self,
        current_summary: Any,
        comparison: Optional[dict[str, Any]],
        projected_bill: Optional[float],
    ) -> list[HouseholdInsight]:
        if not comparison or projected_bill is None:
            return []
        previous = comparison.get("summary_b") or {}
        previous_cost = _number(previous, "net_cost")
        if previous_cost <= 0:
            return []
        diff = float(projected_bill) - previous_cost
        pct = diff / previous_cost * 100
        if abs(diff) < 10 or abs(pct) < 10:
            return []
        direction = "higher" if diff > 0 else "lower"
        return [
            HouseholdInsight(
                insight_id="projected_bill_vs_previous_period",
                headline=f"Your projected bill is {_money(abs(diff))} {direction} than the previous billing period.",
                explanation=(
                    "PowerMonitor compared the current projection with the previous completed billing period. "
                    "That is interesting because it shows whether the bill is drifting before the period closes."
                ),
                comparison_period=str((comparison.get("period_b") or {}).get("label") or "Previous billing period"),
                confidence=_confidence(int(_number(current_summary, "reading_days") + _number(previous, "reading_days"))),
                supporting_values={
                    "Projected bill": _money(float(projected_bill)),
                    "Previous period": _money(previous_cost),
                    "Difference": f"{_money(abs(diff))} {direction}",
                },
                topic="bill_projection",
                usefulness=70 + min(abs(pct), 30),
            )
        ]

    def _peak_usage_insights(self, comparison: Optional[dict[str, Any]]) -> list[HouseholdInsight]:
        if not comparison:
            return []
        deltas = comparison.get("deltas") or {}
        previous = comparison.get("summary_b") or {}
        previous_peak = _number(((previous.get("buckets") or {}).get("peak") or {}), "cost")
        delta = float(deltas.get("peak_cost") or 0.0)
        if previous_peak <= 0 or delta >= -5:
            return []
        pct = abs(delta) / previous_peak * 100
        if pct < 20:
            return []
        return [
            HouseholdInsight(
                insight_id="peak_cost_lower_than_previous_period",
                headline=f"Peak-period cost is {_money(abs(delta))} lower than the comparison period.",
                explanation=(
                    "Peak electricity is usually one of the most expensive parts of the bill. "
                    "A clear drop is interesting even when there is no action to take."
                ),
                comparison_period=str((comparison.get("period_b") or {}).get("label") or "Comparison period"),
                confidence="Medium",
                supporting_values={
                    "Peak cost change": f"-{_money(abs(delta))}",
                    "Previous peak cost": _money(previous_peak),
                    "Change": f"{pct:.0f}% lower",
                },
                topic="peak_usage",
                usefulness=60 + min(pct, 25),
            )
        ]


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _number(item: Any, name: str) -> float:
    try:
        return float(_value(item, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _average(values: Iterable[float]) -> Optional[float]:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _pct_change(current: float, baseline: float) -> float:
    if not baseline:
        return 0.0
    return (current - baseline) / baseline * 100


def _export_pct(trend: Any) -> Optional[float]:
    generated = _number(trend, "solar_generated_kwh")
    if generated <= 0:
        return None
    return _number(trend, "solar_exported_kwh") / generated * 100


def _confidence(sample_count: int) -> str:
    if sample_count >= 14:
        return "High"
    if sample_count >= 6:
        return "Medium"
    return "Low"


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _kwh(value: float) -> str:
    return f"{value:,.1f} kWh"


def _day_label(item: Any) -> str:
    day = _value(item, "day")
    if hasattr(day, "strftime"):
        return day.strftime("%-d %b")
    return str(day or "One day")


def _recommendation_text(recommendations: Iterable[Any]) -> str:
    parts: list[str] = []
    for recommendation in recommendations:
        if isinstance(recommendation, dict):
            values = recommendation.values()
        else:
            values = (
                getattr(recommendation, name, "")
                for name in ("title", "headline", "summary", "recommendation", "explanation")
            )
        parts.extend(str(value) for value in values if value)
    return _normalise(" ".join(parts))


def _duplicates_recommendation(insight: HouseholdInsight, recommendation_text: str) -> bool:
    if not recommendation_text:
        return False
    headline = _normalise(insight.headline)
    important = [word for word in headline.split() if len(word) > 4]
    if len(important) < 3:
        return False
    overlap = sum(1 for word in important if word in recommendation_text)
    return overlap >= min(4, len(important))


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
