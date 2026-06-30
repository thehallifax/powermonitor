#!/usr/bin/env python3
"""Energy Score v1 built from existing PowerMonitor outputs."""

from typing import Any, Iterable

from engine.models import Decision, DecisionExplanation, EnergyScore


def build_energy_score(
    summary: Any,
    decisions: Iterable[Decision],
    data_completeness: dict[str, Any],
    plan_comparison: dict[str, Any],
) -> EnergyScore:
    """Build a simple 0-100 health score without new data access."""

    decision_list = list(decisions)
    score = 100
    positives: list[tuple[int, str]] = []
    negatives: list[tuple[int, str]] = []

    peak_share = float(_value(summary, "peak_cost_share_pct") or 0)
    if peak_share <= 15:
        positives.append((8, f"Peak exposure is low at {peak_share:.1f}% of import cost."))
    elif peak_share <= 25:
        positives.append((4, f"Peak exposure is controlled at {peak_share:.1f}% of import cost."))
    else:
        penalty = 12 if peak_share <= 35 else 20
        score -= penalty
        negatives.append((penalty, f"Peak exposure is high at {peak_share:.1f}% of import cost."))

    solar_self_consumption = float(_value(summary, "self_consumption_pct") or 0)
    if solar_self_consumption >= 75:
        positives.append((10, f"Solar self-consumption is excellent at {solar_self_consumption:.1f}%."))
    elif solar_self_consumption >= 60:
        positives.append((6, f"Solar self-consumption is healthy at {solar_self_consumption:.1f}%."))
    else:
        penalty = 10 if solar_self_consumption >= 45 else 18
        score -= penalty
        negatives.append((penalty, f"Solar self-consumption is low at {solar_self_consumption:.1f}%."))

    ev_kwh = float(_value(summary, "ev_charging_kwh") or 0)
    ev_source = str(_value(summary, "ev_charging_source") or "unknown")
    if ev_kwh > 0 and ev_source in {"ocular_export", "home_assistant_ocpp"}:
        positives.append((6, "EV charging is backed by actual session data."))
    elif ev_kwh > 0:
        score -= 7
        negatives.append((7, "EV charging is using estimated/fallback data."))

    savings = float(plan_comparison.get("savings") or 0)
    if savings > 0:
        positives.append((5, "Current tariff comparison is favourable."))
    elif savings < 0:
        score -= 8
        negatives.append((8, "Comparison tariff is currently ahead."))

    completeness_confidence = str(data_completeness.get("confidence") or "Unknown")
    if completeness_confidence == "Low":
        score -= 12
        negatives.append((12, "Data completeness confidence is low."))
    elif completeness_confidence == "High":
        positives.append((6, "Data completeness confidence is high."))

    trend_decisions = [decision for decision in decision_list if decision.id.startswith("trend_")]
    trend_warnings = [
        decision for decision in trend_decisions
        if decision.priority in {"Medium", "High"} and decision.action_type != "maintain"
    ]
    trend_wins = [
        decision for decision in trend_decisions
        if decision.action_type == "maintain" or decision.priority == "Low"
    ]
    if trend_warnings:
        penalty = min(12, len(trend_warnings) * 6)
        score -= penalty
        negatives.append((penalty, f"{len(trend_warnings)} recent trend warning(s) need attention."))
    if trend_wins:
        positives.append((min(8, len(trend_wins) * 4), f"{len(trend_wins)} recent positive trend(s) are holding."))

    score = _clamp_score(score)
    potential_score = _clamp_score(score + sum(value for value, _label in negatives[:3]))
    confidence = _score_confidence(completeness_confidence, decision_list)
    sources = _data_sources(decision_list, data_completeness)
    top_positive = [label for _value, label in sorted(positives, reverse=True)[:3]]
    top_negative = [label for _value, label in sorted(negatives, reverse=True)[:3]]

    label = _score_label(score)
    summary_text = (
        f"Energy Score is {score}/100 ({label}). "
        f"Potential score is {potential_score}/100 if the top negative factors improve."
    )
    explanation = DecisionExplanation(
        why_this_matters="The score condenses current energy behaviour, data confidence, and recent decision trends into one daily health signal.",
        evidence_used=[
            f"Peak exposure: {peak_share:.1f}%",
            f"Solar self-consumption: {solar_self_consumption:.1f}%",
            f"EV source: {ev_source}",
            f"Data confidence: {completeness_confidence}",
            f"Trend warnings: {len(trend_warnings)}",
            f"Trend wins: {len(trend_wins)}",
        ],
        financial_impact="Uses existing financial and tariff outputs; no new money calculation.",
        confidence_reason=_confidence_reason(confidence, completeness_confidence, decision_list),
        data_sources_used=sources,
        freshness_completeness_note=data_completeness.get("confidence_note"),
    )
    return EnergyScore(
        score=score,
        label=label,
        summary=summary_text,
        top_positive_factors=top_positive,
        top_negative_factors=top_negative,
        potential_score=potential_score,
        confidence=confidence,
        data_sources_used=sources,
        explanation=explanation,
    )


def _value(source: Any, name: str) -> Any:
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name)


def _clamp_score(value: float) -> int:
    return max(0, min(100, round(value)))


def _score_label(score: int) -> str:
    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 50:
        return "Fair"
    return "Poor"


def _score_confidence(completeness_confidence: str, decisions: list[Decision]) -> str:
    if completeness_confidence == "Low":
        return "Low"
    if any(decision.confidence == "Low" for decision in decisions):
        return "Medium"
    if completeness_confidence == "High":
        return "High"
    return "Medium"


def _confidence_reason(confidence: str, completeness_confidence: str, decisions: list[Decision]) -> str:
    return (
        f"{confidence} confidence because data completeness is {completeness_confidence.lower()} "
        f"and {len(decisions)} household recommendation signals were considered."
    )


def _data_sources(decisions: list[Decision], data_completeness: dict[str, Any]) -> list[str]:
    sources = []
    for decision in decisions:
        sources.extend(decision.data_sources_used)
    sources.append("Data completeness summary")
    if data_completeness:
        sources.append("Report summary")
    return list(dict.fromkeys(sources))
