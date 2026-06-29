#!/usr/bin/env python3
"""Detect meaningful changes between Household State snapshots."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class StateChangeType(str, Enum):
    TARIFF_WINDOW_CHANGED = "tariff_window_changed"
    ENTERED_PEAK = "entered_peak"
    EXITED_PEAK = "exited_peak"
    ENTERED_LOW_COST_WINDOW = "entered_low_cost_window"
    SOLAR_STARTED = "solar_started"
    SOLAR_STOPPED = "solar_stopped"
    IMPORT_EXPORT_DIRECTION_CHANGED = "import_export_direction_changed"
    COLLECTOR_WENT_STALE = "collector_went_stale"
    COLLECTOR_RECOVERED = "collector_recovered"
    BILLING_PERIOD_STARTED = "billing_period_started"
    BILLING_PERIOD_NEAR_END = "billing_period_near_end"
    EVIDENCE_QUALITY_DEGRADED = "evidence_quality_degraded"
    EVIDENCE_QUALITY_RECOVERED = "evidence_quality_recovered"
    PROVIDER_DEGRADED = "provider_degraded"
    PROVIDER_RECOVERED = "provider_recovered"


class StateChangeSeverity(str, Enum):
    INFO = "info"
    OPPORTUNITY = "opportunity"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class StateChangeEvidence:
    field: str
    previous: Any
    current: Any
    source: str = "household_state_snapshot"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StateChange:
    change_id: str
    household_id: str
    change_type: StateChangeType
    severity: StateChangeSeverity
    title: str
    summary: str
    detected_at: str
    previous_snapshot_id: str
    current_snapshot_id: str
    evidence: list[StateChangeEvidence] = field(default_factory=list)
    event_key: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "household_id": self.household_id,
            "change_type": self.change_type.value,
            "severity": self.severity.value,
            "title": self.title,
            "summary": self.summary,
            "detected_at": self.detected_at,
            "previous_snapshot_id": self.previous_snapshot_id,
            "current_snapshot_id": self.current_snapshot_id,
            "evidence": [item.as_dict() for item in self.evidence],
            "event_key": self.event_key,
        }


@dataclass(frozen=True)
class StateChangeRule:
    change_type: StateChangeType
    severity: StateChangeSeverity
    field: str
    title: str

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["change_type"] = self.change_type.value
        data["severity"] = self.severity.value
        return data


@dataclass(frozen=True)
class StateChangeDetectionResult:
    household_id: str
    previous_snapshot_id: Optional[str]
    current_snapshot_id: Optional[str]
    changes: list[StateChange] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "household_id": self.household_id,
            "previous_snapshot_id": self.previous_snapshot_id,
            "current_snapshot_id": self.current_snapshot_id,
            "changes": [change.as_dict() for change in self.changes],
            "count": len(self.changes),
        }


class StateChangeEngine:
    """Compares two persisted Household State snapshots."""

    def detect(
        self,
        previous_snapshot: Optional[dict[str, Any]],
        current_snapshot: Optional[dict[str, Any]],
    ) -> StateChangeDetectionResult:
        household_id = (current_snapshot or previous_snapshot or {}).get("household_id") or "unknown_household"
        if not previous_snapshot or not current_snapshot:
            return StateChangeDetectionResult(
                household_id=household_id,
                previous_snapshot_id=previous_snapshot.get("snapshot_id") if previous_snapshot else None,
                current_snapshot_id=current_snapshot.get("snapshot_id") if current_snapshot else None,
                changes=[],
            )
        changes: list[StateChange] = []
        self._tariff_changes(previous_snapshot, current_snapshot, changes)
        self._solar_changes(previous_snapshot, current_snapshot, changes)
        self._power_changes(previous_snapshot, current_snapshot, changes)
        self._collector_changes(previous_snapshot, current_snapshot, changes)
        self._billing_changes(previous_snapshot, current_snapshot, changes)
        self._evidence_changes(previous_snapshot, current_snapshot, changes)
        self._provider_changes(previous_snapshot, current_snapshot, changes)
        return StateChangeDetectionResult(
            household_id=household_id,
            previous_snapshot_id=previous_snapshot["snapshot_id"],
            current_snapshot_id=current_snapshot["snapshot_id"],
            changes=changes,
        )

    def _change(
        self,
        previous_snapshot: dict[str, Any],
        current_snapshot: dict[str, Any],
        change_type: StateChangeType,
        severity: StateChangeSeverity,
        title: str,
        summary: str,
        evidence: list[StateChangeEvidence],
    ) -> StateChange:
        event_key = "|".join([
            current_snapshot["household_id"],
            change_type.value,
            current_snapshot["snapshot_id"],
        ])
        return StateChange(
            change_id=event_key,
            household_id=current_snapshot["household_id"],
            change_type=change_type,
            severity=severity,
            title=title,
            summary=summary,
            detected_at=current_snapshot["captured_at"],
            previous_snapshot_id=previous_snapshot["snapshot_id"],
            current_snapshot_id=current_snapshot["snapshot_id"],
            evidence=evidence,
            event_key=event_key,
        )

    def _tariff_changes(self, previous: dict[str, Any], current: dict[str, Any], changes: list[StateChange]) -> None:
        previous_bucket = previous.get("tariff_state", {}).get("active_bucket")
        current_bucket = current.get("tariff_state", {}).get("active_bucket")
        if previous_bucket == current_bucket:
            return
        evidence = [StateChangeEvidence("tariff.active_bucket", previous_bucket, current_bucket)]
        changes.append(self._change(previous, current, StateChangeType.TARIFF_WINDOW_CHANGED, StateChangeSeverity.INFO, "Tariff window changed", f"Tariff changed from {previous_bucket} to {current_bucket}.", evidence))
        if current_bucket == "peak":
            changes.append(self._change(previous, current, StateChangeType.ENTERED_PEAK, StateChangeSeverity.WARNING, "Entered Peak tariff", "The household entered a Peak tariff window.", evidence))
        if previous_bucket == "peak" and current_bucket != "peak":
            changes.append(self._change(previous, current, StateChangeType.EXITED_PEAK, StateChangeSeverity.INFO, "Exited Peak tariff", "The household exited the Peak tariff window.", evidence))
        if current_bucket in {"super_off_peak", "overnight"}:
            changes.append(self._change(previous, current, StateChangeType.ENTERED_LOW_COST_WINDOW, StateChangeSeverity.OPPORTUNITY, "Entered low-cost tariff window", "A lower-cost tariff window is now active.", evidence))

    def _solar_changes(self, previous: dict[str, Any], current: dict[str, Any], changes: list[StateChange]) -> None:
        previous_producing = previous.get("solar_state", {}).get("producing")
        current_producing = current.get("solar_state", {}).get("producing")
        evidence = [StateChangeEvidence("solar.producing", previous_producing, current_producing)]
        if previous_producing is False and current_producing is True:
            changes.append(self._change(previous, current, StateChangeType.SOLAR_STARTED, StateChangeSeverity.OPPORTUNITY, "Solar started", "Solar production is now available.", evidence))
        if previous_producing is True and current_producing is False:
            changes.append(self._change(previous, current, StateChangeType.SOLAR_STOPPED, StateChangeSeverity.INFO, "Solar stopped", "Solar production is no longer active.", evidence))

    def _power_changes(self, previous: dict[str, Any], current: dict[str, Any], changes: list[StateChange]) -> None:
        previous_direction = previous.get("power_state", {}).get("import_export_state")
        current_direction = current.get("power_state", {}).get("import_export_state")
        if previous_direction and current_direction and previous_direction != current_direction:
            changes.append(self._change(
                previous,
                current,
                StateChangeType.IMPORT_EXPORT_DIRECTION_CHANGED,
                StateChangeSeverity.INFO,
                "Import/export direction changed",
                f"Power direction changed from {previous_direction} to {current_direction}.",
                [StateChangeEvidence("power.import_export_state", previous_direction, current_direction)],
            ))

    def _collector_changes(self, previous: dict[str, Any], current: dict[str, Any], changes: list[StateChange]) -> None:
        previous_healthy = previous.get("collector_state", {}).get("healthy")
        current_healthy = current.get("collector_state", {}).get("healthy")
        evidence = [StateChangeEvidence("collector.healthy", previous_healthy, current_healthy)]
        if previous_healthy is True and current_healthy is False:
            changes.append(self._change(previous, current, StateChangeType.COLLECTOR_WENT_STALE, StateChangeSeverity.CRITICAL, "Collector went stale", "Collector health degraded.", evidence))
        if previous_healthy is False and current_healthy is True:
            changes.append(self._change(previous, current, StateChangeType.COLLECTOR_RECOVERED, StateChangeSeverity.INFO, "Collector recovered", "Collector health recovered.", evidence))

    def _billing_changes(self, previous: dict[str, Any], current: dict[str, Any], changes: list[StateChange]) -> None:
        previous_start = previous.get("billing_state", {}).get("period_start")
        current_start = current.get("billing_state", {}).get("period_start")
        if previous_start and current_start and previous_start != current_start:
            changes.append(self._change(previous, current, StateChangeType.BILLING_PERIOD_STARTED, StateChangeSeverity.INFO, "Billing period started", "A new billing period is active.", [StateChangeEvidence("billing.period_start", previous_start, current_start)]))
        supply_days = int(current.get("billing_state", {}).get("supply_days") or 0)
        day = int(current.get("billing_state", {}).get("day_of_period") or 0)
        previous_day = int(previous.get("billing_state", {}).get("day_of_period") or 0)
        if supply_days and day >= max(supply_days - 3, 1) and previous_day < max(supply_days - 3, 1):
            changes.append(self._change(previous, current, StateChangeType.BILLING_PERIOD_NEAR_END, StateChangeSeverity.WARNING, "Billing period near end", "The billing period is nearing its end.", [StateChangeEvidence("billing.day_of_period", previous_day, day)]))

    def _evidence_changes(self, previous: dict[str, Any], current: dict[str, Any], changes: list[StateChange]) -> None:
        rank = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
        previous_quality = previous.get("evidence_state", {}).get("confidence")
        current_quality = current.get("evidence_state", {}).get("confidence")
        if previous_quality == current_quality:
            return
        evidence = [StateChangeEvidence("evidence.confidence", previous_quality, current_quality)]
        if rank.get(str(current_quality), 0) < rank.get(str(previous_quality), 0):
            changes.append(self._change(previous, current, StateChangeType.EVIDENCE_QUALITY_DEGRADED, StateChangeSeverity.WARNING, "Evidence quality degraded", "Household evidence confidence decreased.", evidence))
        else:
            changes.append(self._change(previous, current, StateChangeType.EVIDENCE_QUALITY_RECOVERED, StateChangeSeverity.INFO, "Evidence quality recovered", "Household evidence confidence improved.", evidence))

    def _provider_changes(self, previous: dict[str, Any], current: dict[str, Any], changes: list[StateChange]) -> None:
        previous_providers = set(previous.get("provider_provenance", {}).get("trusted_provider_data", []))
        current_providers = set(current.get("provider_provenance", {}).get("trusted_provider_data", []))
        lost = sorted(previous_providers - current_providers)
        recovered = sorted(current_providers - previous_providers)
        if lost:
            changes.append(self._change(previous, current, StateChangeType.PROVIDER_DEGRADED, StateChangeSeverity.WARNING, "Provider degraded", f"Trusted provider data is no longer present: {', '.join(lost)}.", [StateChangeEvidence("provider.trusted_provider_data", sorted(previous_providers), sorted(current_providers))]))
        if recovered:
            changes.append(self._change(previous, current, StateChangeType.PROVIDER_RECOVERED, StateChangeSeverity.INFO, "Provider recovered", f"Trusted provider data is now present: {', '.join(recovered)}.", [StateChangeEvidence("provider.trusted_provider_data", sorted(previous_providers), sorted(current_providers))]))
