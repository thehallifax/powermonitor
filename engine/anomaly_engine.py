#!/usr/bin/env python3
"""Behaviour-aware household anomaly detection for PowerMonitor.

The Anomaly Engine is descriptive only. It identifies unusual behaviour
relative to learned household behaviour and never recommends or optimises.
"""

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class AnomalyCategory(str, Enum):
    BASELOAD = "baseload"
    EV_CHARGING = "ev_charging"
    SOLAR = "solar"
    BILLING = "billing"
    COLLECTOR = "collector"


class AnomalySeverity(str, Enum):
    INFO = "info"
    WATCH = "watch"
    WARNING = "warning"
    CRITICAL = "critical"


class AnomalyConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AnomalyEvidence:
    source: str
    summary: str
    observed: Any = None
    expected: Any = None
    observed_at: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HouseholdAnomaly:
    anomaly_id: str
    household_id: str
    category: AnomalyCategory
    title: str
    summary: str
    severity: AnomalySeverity
    confidence: AnomalyConfidence
    first_detected: str
    evidence: list[AnomalyEvidence] = field(default_factory=list)
    data_sources_used: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "anomaly_id": self.anomaly_id,
            "household_id": self.household_id,
            "category": self.category.value,
            "title": self.title,
            "summary": self.summary,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "first_detected": self.first_detected,
            "evidence": [item.as_dict() for item in self.evidence],
            "data_sources_used": list(self.data_sources_used),
        }


class AnomalyEngine:
    """Compares current household signals against learned behaviour."""

    def detect(
        self,
        *,
        household_state: Any,
        context: Any,
        state_changes: list[Any],
        behaviour: Any,
        current_observations: Optional[dict[str, Any]] = None,
    ) -> list[HouseholdAnomaly]:
        state = model_dict(household_state)
        context_data = model_dict(context)
        behaviour_data = model_dict(behaviour)
        observations = current_observations or {}
        household_id = str(state.get("household_id") or behaviour_data.get("household_id") or "default_household")
        detected_at = str(state.get("generated_at") or context_data.get("observed_at") or behaviour_data.get("generated_at") or "")
        confidence = confidence_from_behaviour(behaviour_data)
        anomalies: list[HouseholdAnomaly] = []
        self._baseload_anomalies(anomalies, household_id, detected_at, confidence, behaviour_data, observations)
        self._ev_anomalies(anomalies, household_id, detected_at, confidence, behaviour_data, state, context_data, observations)
        self._solar_anomalies(anomalies, household_id, detected_at, confidence, behaviour_data, state, context_data)
        self._billing_anomalies(anomalies, household_id, detected_at, confidence, behaviour_data, state, observations)
        self._collector_anomalies(anomalies, household_id, detected_at, behaviour_data, state, state_changes)
        return anomalies

    def _baseload_anomalies(
        self,
        anomalies: list[HouseholdAnomaly],
        household_id: str,
        detected_at: str,
        confidence: AnomalyConfidence,
        behaviour: dict[str, Any],
        observations: dict[str, Any],
    ) -> None:
        baseline = nested_number(behaviour, "baseload", "overnight_baseload_watts")
        observed = number(observations.get("overnight_baseload_watts"))
        if baseline is None or observed is None or observed <= 0:
            return
        variance = nested_number(behaviour, "baseload", "normal_variance_watts") or baseline * 0.15
        high_limit = baseline + max(variance * 2, baseline * 0.25)
        low_limit = max(baseline - max(variance * 2, baseline * 0.25), 0)
        if observed > high_limit:
            anomalies.append(self._anomaly(
                household_id,
                AnomalyCategory.BASELOAD,
                "Higher than normal overnight load",
                f"Overnight baseload is {observed:.0f} W versus a normal {baseline:.0f} W.",
                AnomalySeverity.WARNING,
                confidence,
                detected_at,
                [AnomalyEvidence("Household Behaviour", "Current overnight baseload is above the learned normal range.", round(observed, 1), round(baseline, 1), detected_at)],
            ))
        elif observed < low_limit:
            anomalies.append(self._anomaly(
                household_id,
                AnomalyCategory.BASELOAD,
                "Lower than normal overnight load",
                f"Overnight baseload is {observed:.0f} W versus a normal {baseline:.0f} W.",
                AnomalySeverity.INFO,
                confidence,
                detected_at,
                [AnomalyEvidence("Household Behaviour", "Current overnight baseload is below the learned normal range.", round(observed, 1), round(baseline, 1), detected_at)],
            ))
        self._persistent_change(anomalies, household_id, detected_at, confidence, observations, "overnight_baseload_trend", baseline)

    def _persistent_change(
        self,
        anomalies: list[HouseholdAnomaly],
        household_id: str,
        detected_at: str,
        confidence: AnomalyConfidence,
        observations: dict[str, Any],
        key: str,
        baseline: float,
    ) -> None:
        trend = observations.get(key)
        if not isinstance(trend, dict):
            return
        direction = str(trend.get("direction") or "")
        observed = number(trend.get("current"))
        if direction not in {"increase", "decrease"} or observed is None:
            return
        title = "Persistent overnight increase" if direction == "increase" else "Persistent overnight decrease"
        severity = AnomalySeverity.WARNING if direction == "increase" else AnomalySeverity.INFO
        anomalies.append(self._anomaly(
            household_id,
            AnomalyCategory.BASELOAD,
            title,
            f"Recent overnight baseload is persistently moving {direction} from the learned normal.",
            severity,
            confidence,
            detected_at,
            [AnomalyEvidence("Household Behaviour", "Baseload trend differs from normal behaviour.", observed, baseline, detected_at)],
        ))

    def _ev_anomalies(
        self,
        anomalies: list[HouseholdAnomaly],
        household_id: str,
        detected_at: str,
        confidence: AnomalyConfidence,
        behaviour: dict[str, Any],
        state: dict[str, Any],
        context: dict[str, Any],
        observations: dict[str, Any],
    ) -> None:
        ev = behaviour.get("ev") or {}
        normal_window = ev.get("preferred_charging_window")
        current_window = observations.get("current_ev_charging_window")
        if normal_window and current_window and not windows_overlap(str(normal_window), str(current_window)):
            anomalies.append(self._anomaly(
                household_id,
                AnomalyCategory.EV_CHARGING,
                "EV charging outside normal hours",
                f"EV charging is occurring around {current_window}, outside the usual {normal_window} window.",
                AnomalySeverity.WATCH,
                confidence,
                detected_at,
                [AnomalyEvidence("EV behaviour profile", "Charging window differs from learned EV behaviour.", current_window, normal_window, detected_at)],
            ))
        charging_state = str(context.get("charging") or "")
        if normal_window and charging_state == "UNKNOWN" and observations.get("missed_normal_charging_window"):
            anomalies.append(self._anomaly(
                household_id,
                AnomalyCategory.EV_CHARGING,
                "Missed normal charging window",
                f"The household appears to have missed its usual EV charging window of {normal_window}.",
                AnomalySeverity.WATCH,
                confidence,
                detected_at,
                [AnomalyEvidence("EV behaviour profile", "Expected EV charging window passed without matching activity.", charging_state, normal_window, detected_at)],
            ))
        normal_session = number(observations.get("normal_ev_session_kwh"))
        current_session = number(observations.get("current_ev_session_kwh"))
        if normal_session and current_session:
            if current_session > normal_session * 1.6:
                anomalies.append(self._anomaly(
                    household_id,
                    AnomalyCategory.EV_CHARGING,
                    "Unusually large EV charging session",
                    f"Current EV session is {current_session:.1f} kWh versus a normal {normal_session:.1f} kWh.",
                    AnomalySeverity.WATCH,
                    confidence,
                    detected_at,
                    [AnomalyEvidence("EV behaviour profile", "Session size is larger than learned EV behaviour.", current_session, normal_session, detected_at)],
                ))
            elif current_session < normal_session * 0.4:
                anomalies.append(self._anomaly(
                    household_id,
                    AnomalyCategory.EV_CHARGING,
                    "Unusually small EV charging session",
                    f"Current EV session is {current_session:.1f} kWh versus a normal {normal_session:.1f} kWh.",
                    AnomalySeverity.INFO,
                    confidence,
                    detected_at,
                    [AnomalyEvidence("EV behaviour profile", "Session size is smaller than learned EV behaviour.", current_session, normal_session, detected_at)],
                ))

    def _solar_anomalies(
        self,
        anomalies: list[HouseholdAnomaly],
        household_id: str,
        detected_at: str,
        confidence: AnomalyConfidence,
        behaviour: dict[str, Any],
        state: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        solar = behaviour.get("solar") or {}
        energy = state.get("energy") or {}
        current_export_pct = pct(number(energy.get("solar_exported_kwh_today")), number(energy.get("solar_generated_kwh_today")))
        normal_sunny_export = number(solar.get("sunny_day_export_pct"))
        if current_export_pct is not None and normal_sunny_export is not None:
            if current_export_pct > normal_sunny_export + 25:
                anomalies.append(self._anomaly(
                    household_id,
                    AnomalyCategory.SOLAR,
                    "Unusually high solar exports",
                    f"Solar export share is {current_export_pct:.1f}% versus a normal sunny-day {normal_sunny_export:.1f}%.",
                    AnomalySeverity.WATCH,
                    confidence,
                    detected_at,
                    [AnomalyEvidence("Solar behaviour profile", "Current export share is above learned solar behaviour.", round(current_export_pct, 1), normal_sunny_export, detected_at)],
                ))
            elif current_export_pct < max(normal_sunny_export - 25, 0):
                anomalies.append(self._anomaly(
                    household_id,
                    AnomalyCategory.SOLAR,
                    "Unusually low solar exports",
                    f"Solar export share is {current_export_pct:.1f}% versus a normal sunny-day {normal_sunny_export:.1f}%.",
                    AnomalySeverity.INFO,
                    confidence,
                    detected_at,
                    [AnomalyEvidence("Solar behaviour profile", "Current export share is below learned solar behaviour.", round(current_export_pct, 1), normal_sunny_export, detected_at)],
                ))
        self_consumption = pct(number(energy.get("solar_used_kwh_today")), number(energy.get("solar_generated_kwh_today")))
        if self_consumption is not None and str(solar.get("self_consumption_tendency")) == "high" and self_consumption < 45:
            anomalies.append(self._anomaly(
                household_id,
                AnomalyCategory.SOLAR,
                "Reduced solar self-consumption",
                f"Solar self-consumption is {self_consumption:.1f}%, below this household's normal tendency.",
                AnomalySeverity.WATCH,
                confidence,
                detected_at,
                [AnomalyEvidence("Solar behaviour profile", "Self-consumption is lower than learned behaviour.", round(self_consumption, 1), "high tendency", detected_at)],
            ))
        if str(context.get("solar")) in {"SOLAR_AVAILABLE", "SOLAR_SURPLUS_POSSIBLE"} and str((state.get("power") or {}).get("import_export_state")) == "importing":
            anomalies.append(self._anomaly(
                household_id,
                AnomalyCategory.SOLAR,
                "Unexpected daytime imports",
                "The household is importing while solar context indicates solar should be available.",
                AnomalySeverity.WATCH,
                confidence,
                detected_at,
                [AnomalyEvidence("Household State", "Current power direction differs from learned solar-use context.", "importing", "solar available", detected_at)],
            ))

    def _billing_anomalies(
        self,
        anomalies: list[HouseholdAnomaly],
        household_id: str,
        detected_at: str,
        confidence: AnomalyConfidence,
        behaviour: dict[str, Any],
        state: dict[str, Any],
        observations: dict[str, Any],
    ) -> None:
        expected_daily = number(observations.get("normal_daily_cost"))
        current_daily = number(observations.get("current_daily_cost"))
        if expected_daily and current_daily:
            if current_daily > expected_daily * 1.5:
                anomalies.append(self._anomaly(
                    household_id,
                    AnomalyCategory.BILLING,
                    "Higher than expected daily cost",
                    f"Daily cost is {current_daily:.2f} versus a normal {expected_daily:.2f}.",
                    AnomalySeverity.WARNING,
                    confidence,
                    detected_at,
                    [AnomalyEvidence("Billing behaviour", "Current daily cost is above learned cost behaviour.", round(current_daily, 2), round(expected_daily, 2), detected_at)],
                ))
            elif current_daily < expected_daily * 0.5:
                anomalies.append(self._anomaly(
                    household_id,
                    AnomalyCategory.BILLING,
                    "Lower than expected daily cost",
                    f"Daily cost is {current_daily:.2f} versus a normal {expected_daily:.2f}.",
                    AnomalySeverity.INFO,
                    confidence,
                    detected_at,
                    [AnomalyEvidence("Billing behaviour", "Current daily cost is below learned cost behaviour.", round(current_daily, 2), round(expected_daily, 2), detected_at)],
                ))
        trend = str(observations.get("billing_trend") or "")
        if trend in {"higher_than_normal", "lower_than_normal"}:
            anomalies.append(self._anomaly(
                household_id,
                AnomalyCategory.BILLING,
                "Spending diverging from normal trend",
                f"Billing spend is tracking {trend.replace('_', ' ')}.",
                AnomalySeverity.WARNING if trend == "higher_than_normal" else AnomalySeverity.INFO,
                confidence,
                detected_at,
                [AnomalyEvidence("Billing behaviour", "Billing trend differs from learned behaviour.", trend, "normal trend", detected_at)],
            ))

    def _collector_anomalies(
        self,
        anomalies: list[HouseholdAnomaly],
        household_id: str,
        detected_at: str,
        behaviour: dict[str, Any],
        state: dict[str, Any],
        state_changes: list[Any],
    ) -> None:
        evidence = state.get("evidence") or {}
        if str(evidence.get("confidence")) in {"LOW", "UNKNOWN"} and confidence_from_behaviour(behaviour) in {AnomalyConfidence.HIGH, AnomalyConfidence.MEDIUM}:
            anomalies.append(self._anomaly(
                household_id,
                AnomalyCategory.COLLECTOR,
                "Unusual evidence degradation",
                "Current evidence quality is degraded compared with normally usable household history.",
                AnomalySeverity.WARNING,
                AnomalyConfidence.MEDIUM,
                detected_at,
                [AnomalyEvidence("Household State", "Evidence confidence is lower than normal behaviour profile confidence.", evidence.get("confidence"), behaviour.get("confidence"), detected_at)],
            ))
        for change in state_changes:
            change_type = str(get_value(change, "change_type") or "")
            if change_type in {"collector_went_stale", "provider_degraded"}:
                anomalies.append(self._anomaly(
                    household_id,
                    AnomalyCategory.COLLECTOR,
                    "Unexpected provider behaviour",
                    str(get_value(change, "summary") or "Provider or collector state changed unexpectedly."),
                    AnomalySeverity.WARNING,
                    AnomalyConfidence.MEDIUM,
                    str(get_value(change, "detected_at") or detected_at),
                    [AnomalyEvidence("State Change Engine", str(get_value(change, "title") or change_type), change_type, "normal provider operation", str(get_value(change, "detected_at") or detected_at))],
                ))

    def _anomaly(
        self,
        household_id: str,
        category: AnomalyCategory,
        title: str,
        summary: str,
        severity: AnomalySeverity,
        confidence: AnomalyConfidence,
        detected_at: str,
        evidence: list[AnomalyEvidence],
    ) -> HouseholdAnomaly:
        basis = "|".join([household_id, category.value, title, detected_at])
        return HouseholdAnomaly(
            anomaly_id=hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32],
            household_id=household_id,
            category=category,
            title=title,
            summary=summary,
            severity=severity,
            confidence=confidence,
            first_detected=detected_at,
            evidence=evidence,
            data_sources_used=sorted({item.source for item in evidence}),
        )


def model_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "as_dict") and callable(value.as_dict):
        return value.as_dict()
    if isinstance(value, dict):
        return value
    return {}


def get_value(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def nested_number(data: dict[str, Any], *keys: str) -> Optional[float]:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return number(current)


def pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator * 100


def confidence_from_behaviour(behaviour: dict[str, Any]) -> AnomalyConfidence:
    value = str(behaviour.get("confidence") or "UNKNOWN")
    try:
        return AnomalyConfidence(value)
    except ValueError:
        return AnomalyConfidence.UNKNOWN


def windows_overlap(left: str, right: str) -> bool:
    left_start = parse_window_start(left)
    right_start = parse_window_start(right)
    if left_start is None or right_start is None:
        return left == right
    distance = abs(left_start - right_start)
    distance = min(distance, 24 - distance)
    return distance <= 1


def parse_window_start(value: str) -> Optional[int]:
    try:
        return int(str(value).split(":", 1)[0])
    except (TypeError, ValueError):
        return None
