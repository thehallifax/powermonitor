#!/usr/bin/env python3
"""Promotion rules for turning provider observations into trusted read models."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class PromotionState(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    PENDING = "PENDING"


@dataclass(frozen=True)
class PromotionReason:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ObservationValidation:
    valid: bool
    reasons: list[PromotionReason] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "reasons": [reason.as_dict() for reason in self.reasons],
        }


@dataclass(frozen=True)
class ObservationConflict:
    observation_id: str
    conflicting_observation_id: str
    observation_type: str
    difference: float
    tolerance: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromotionCandidate:
    observation_id: str
    household_id: str
    provider_id: str
    provider_type: str
    capability: str
    observation_type: str
    observed_at: str
    value: Any
    quality: str
    unit: Optional[str] = None
    received_at: Optional[str] = None
    freshness: Optional[dict[str, Any]] = None
    raw_reference: Optional[str] = None

    @classmethod
    def from_observation(cls, observation: dict[str, Any]) -> "PromotionCandidate":
        return cls(
            observation_id=str(observation.get("observation_id") or ""),
            household_id=str(observation.get("household_id") or ""),
            provider_id=str(observation.get("provider_id") or ""),
            provider_type=str(observation.get("provider_type") or ""),
            capability=str(observation.get("capability") or ""),
            observation_type=str(observation.get("observation_type") or ""),
            observed_at=str(observation.get("observed_at") or ""),
            value=observation.get("value"),
            quality=str(observation.get("quality") or "unknown"),
            unit=observation.get("unit"),
            received_at=observation.get("received_at"),
            freshness=observation.get("freshness") if isinstance(observation.get("freshness"), dict) else None,
            raw_reference=observation.get("raw_reference"),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromotionDecision:
    state: PromotionState
    reasons: list[PromotionReason] = field(default_factory=list)
    conflict: Optional[ObservationConflict] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reasons": [reason.as_dict() for reason in self.reasons],
            "conflict": self.conflict.as_dict() if self.conflict else None,
        }


@dataclass(frozen=True)
class PromotionResult:
    candidate: PromotionCandidate
    decision: PromotionDecision
    promoted_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.as_dict(),
            "decision": self.decision.as_dict(),
            "promoted_at": self.promoted_at,
        }


class PromotionEngine:
    """Validate, deduplicate, and resolve conflicts between provider observations."""

    name = "promotion_engine"

    def __init__(self, tolerance_by_type: Optional[dict[str, float]] = None) -> None:
        self.tolerance_by_type = {
            "solar_generation": 0.05,
            "solar_export": 0.05,
            "grid_import": 0.05,
            "ev_session": 0.05,
            **(tolerance_by_type or {}),
        }

    def evaluate(
        self,
        observations: list[dict[str, Any]],
        *,
        household_id: Optional[str] = None,
        promoted_at: Optional[str] = None,
    ) -> list[PromotionResult]:
        promoted_at = promoted_at or datetime.now().astimezone().isoformat(timespec="seconds")
        candidates = [PromotionCandidate.from_observation(item) for item in observations]
        results: dict[str, PromotionResult] = {}
        seen_signatures: dict[str, str] = {}
        newest_by_source: dict[str, PromotionCandidate] = {}
        accepted_by_slot: dict[str, PromotionCandidate] = {}

        for candidate in sorted(candidates, key=lambda item: (item.observed_at, item.observation_id)):
            validation = self.validate(candidate, household_id=household_id)
            if not validation.valid:
                results[candidate.observation_id] = self._result(
                    candidate,
                    PromotionState.REJECTED,
                    validation.reasons,
                    promoted_at,
                )
                continue

            signature = self._duplicate_signature(candidate)
            if signature in seen_signatures:
                results[candidate.observation_id] = self._result(
                    candidate,
                    PromotionState.DUPLICATE,
                    [PromotionReason("duplicate", "An identical provider observation already exists.")],
                    promoted_at,
                )
                continue
            seen_signatures[signature] = candidate.observation_id

            source_key = self._source_key(candidate)
            previous = newest_by_source.get(source_key)
            if previous and _parse_dt(candidate.observed_at) > _parse_dt(previous.observed_at):
                results[previous.observation_id] = self._result(
                    previous,
                    PromotionState.SUPERSEDED,
                    [PromotionReason("newer_same_provider", "A newer observation from the same provider supersedes this one.")],
                    promoted_at,
                )
            newest_by_source[source_key] = candidate

            conflict = self._conflict(candidate, accepted_by_slot)
            if conflict:
                results[candidate.observation_id] = self._result(
                    candidate,
                    PromotionState.CONFLICT,
                    [PromotionReason("provider_conflict", "Another provider reports a conflicting value outside tolerance.")],
                    promoted_at,
                    conflict=conflict,
                )
                continue

            freshness_state = str((candidate.freshness or {}).get("state") or "").lower()
            if freshness_state == "stale":
                results[candidate.observation_id] = self._result(
                    candidate,
                    PromotionState.PENDING,
                    [PromotionReason("stale_observation", "Observation freshness is stale and needs corroboration before promotion.")],
                    promoted_at,
                )
                continue

            if candidate.quality not in {"high", "medium"}:
                results[candidate.observation_id] = self._result(
                    candidate,
                    PromotionState.PENDING,
                    [PromotionReason("low_observation_quality", "Observation quality is not strong enough for automatic promotion.")],
                    promoted_at,
                )
                continue

            accepted_by_slot[self._conflict_slot(candidate)] = candidate
            results[candidate.observation_id] = self._result(
                candidate,
                PromotionState.ACCEPTED,
                [PromotionReason("valid", "Observation passed validation and promotion checks.")],
                promoted_at,
            )

        return [results[item.observation_id] for item in candidates if item.observation_id in results]

    def validate(self, candidate: PromotionCandidate, *, household_id: Optional[str] = None) -> ObservationValidation:
        reasons = []
        if household_id and candidate.household_id != household_id:
            reasons.append(PromotionReason("household_scope", "Observation belongs to a different household."))
        if not candidate.provider_id:
            reasons.append(PromotionReason("missing_provider", "Observation has no provider id."))
        if candidate.capability not in _CAPABILITY_COMPATIBILITY.get(candidate.observation_type, set()):
            reasons.append(PromotionReason("capability_mismatch", "Observation capability does not match its observation type."))
        if _parse_dt(candidate.observed_at) == datetime.min:
            reasons.append(PromotionReason("invalid_timestamp", "Observation timestamp is missing or invalid."))
        if str((candidate.freshness or {}).get("state") or "").lower() == "expired":
            reasons.append(PromotionReason("expired_observation", "Observation freshness is expired."))
        if candidate.observation_type in _LIVE_OBSERVATION_TYPES and str((candidate.freshness or {}).get("state") or "").lower() in {"stale", "expired"}:
            reasons.append(PromotionReason("stale_live_observation", "Live observation is stale and cannot represent current state."))
        if candidate.observation_type in _UNIT_RULES and _normalised_unit(candidate.unit) not in _UNIT_RULES[candidate.observation_type]:
            reasons.append(PromotionReason("invalid_unit", "Observation unit is not valid for this live observation type."))
        if candidate.observation_type == "battery_soc":
            numeric = _numeric_value(candidate)
            if numeric is None or numeric < 0 or numeric > 100:
                reasons.append(PromotionReason("invalid_battery_soc", "Battery state of charge must be between 0 and 100%."))
        if self._is_impossible_negative(candidate):
            reasons.append(PromotionReason("impossible_negative", "Observation reports an impossible negative energy value."))
        return ObservationValidation(valid=not reasons, reasons=reasons)

    def _conflict(self, candidate: PromotionCandidate, accepted_by_slot: dict[str, PromotionCandidate]) -> Optional[ObservationConflict]:
        numeric = _numeric_value(candidate)
        if numeric is None:
            return None
        existing = accepted_by_slot.get(self._conflict_slot(candidate))
        if not existing or existing.provider_id == candidate.provider_id:
            return None
        existing_value = _numeric_value(existing)
        if existing_value is None:
            return None
        tolerance = self.tolerance_by_type.get(candidate.observation_type, 0.05)
        difference = abs(numeric - existing_value)
        if difference <= tolerance:
            return None
        return ObservationConflict(
            observation_id=candidate.observation_id,
            conflicting_observation_id=existing.observation_id,
            observation_type=candidate.observation_type,
            difference=round(difference, 6),
            tolerance=tolerance,
        )

    def _is_impossible_negative(self, candidate: PromotionCandidate) -> bool:
        if candidate.observation_type not in _NON_NEGATIVE_TYPES:
            return False
        numeric = _numeric_value(candidate)
        return numeric is not None and numeric < 0

    def _duplicate_signature(self, candidate: PromotionCandidate) -> str:
        return "|".join([
            candidate.household_id,
            candidate.provider_id,
            candidate.observation_type,
            candidate.observed_at,
            _stable_value(candidate.value),
            str(candidate.raw_reference or ""),
        ])

    def _source_key(self, candidate: PromotionCandidate) -> str:
        return "|".join([
            candidate.household_id,
            candidate.provider_id,
            candidate.observation_type,
            str(candidate.raw_reference or candidate.capability),
        ])

    def _conflict_slot(self, candidate: PromotionCandidate) -> str:
        return "|".join([candidate.household_id, candidate.observation_type, candidate.observed_at])

    def _result(
        self,
        candidate: PromotionCandidate,
        state: PromotionState,
        reasons: list[PromotionReason],
        promoted_at: str,
        *,
        conflict: Optional[ObservationConflict] = None,
    ) -> PromotionResult:
        return PromotionResult(
            candidate=candidate,
            decision=PromotionDecision(state, reasons, conflict),
            promoted_at=promoted_at,
        )


_CAPABILITY_COMPATIBILITY = {
    "solar_generation": {"daily_summary", "interval_history"},
    "grid_import": {"daily_summary", "interval_history"},
    "solar_export": {"daily_summary", "interval_history"},
    "ev_session": {"ev_sessions", "import_only"},
    "tariff_config": {"tariff_data"},
    "billing_period": {"billing_data"},
    "collector_health": {"import_only", "live_power"},
    "live_power": {"live_power"},
    "grid_live_power": {"live_power", "interval_history"},
    "solar_live_power": {"live_power", "daily_summary", "interval_history"},
    "battery_state": {"battery_state"},
    "battery_soc": {"battery_state"},
    "battery_power": {"battery_state", "live_power"},
    "ev_charger_live_power": {"ev_sessions", "live_power"},
    "ev_connected_state": {"ev_sessions"},
    "ev_charging_state": {"ev_sessions"},
    "home_live_power": {"live_power"},
}

_LIVE_OBSERVATION_TYPES = {
    "live_power",
    "grid_live_power",
    "solar_live_power",
    "battery_state",
    "battery_soc",
    "battery_power",
    "ev_charger_live_power",
    "ev_connected_state",
    "ev_charging_state",
    "home_live_power",
}

_SIGNED_POWER_TYPES = {"grid_live_power", "battery_power"}
_NON_NEGATIVE_TYPES = {
    "solar_generation",
    "solar_export",
    "grid_import",
    "ev_session",
    "live_power",
    "solar_live_power",
    "battery_soc",
    "ev_charger_live_power",
    "home_live_power",
}
_UNIT_RULES = {
    "live_power": {"w", "kw"},
    "grid_live_power": {"w", "kw"},
    "solar_live_power": {"w", "kw"},
    "battery_power": {"w", "kw"},
    "ev_charger_live_power": {"w", "kw"},
    "home_live_power": {"w", "kw"},
    "battery_soc": {"%", "percent"},
    "ev_connected_state": {"state", ""},
    "ev_charging_state": {"state", ""},
    "battery_state": {"state", "%", "percent"},
}


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min


def _stable_value(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True)
    except TypeError:
        return str(value)


def _numeric_value(candidate: PromotionCandidate) -> Optional[float]:
    value = candidate.value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("kwh", "energy_kwh", "generation_kwh", "grid_import_kwh", "solar_export_kwh", "value"):
            if key in value:
                try:
                    return float(value[key])
                except (TypeError, ValueError):
                    return None
    return None


def _normalised_unit(unit: Optional[str]) -> str:
    return str(unit or "").strip().lower()
