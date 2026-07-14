#!/usr/bin/env python3
"""Provider-neutral household timeline and tariff-period resolution."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .tariff_catalog import DEFAULT_PLAN, MANUAL_TARIFF_ID, SUPPORTED_PLANS, plan_label
from .tariff_versioning import TariffResolver, TariffVersion, TariffVersionError, resolver_for_settings

DEFAULT_HOUSEHOLD_TIMELINE_PATH = Path(__file__).resolve().parents[1] / "config" / "household_timeline.json"
DEFAULT_HOUSEHOLD_TIMELINE_AUDIT_PATH = Path(__file__).resolve().parents[1] / "config" / "household_timeline_audit.jsonl"

HOUSEHOLD_EVENT_TYPES = {
    "tariff_period",
    "solar_installation",
    "battery_installation",
    "ev_added",
    "ev_removed",
    "ev_charger_installed",
    "major_appliance_change",
    "occupancy_change",
    "custom_note",
}


class HouseholdTimelineError(ValueError):
    """Raised when a household timeline event cannot be stored or resolved."""


@dataclass(frozen=True)
class HouseholdTimelineEvent:
    event_id: str
    event_type: str
    title: str
    effective_from: date
    effective_to: Optional[date] = None
    source: str = "manual"
    confidence: str = "medium"
    notes: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    active: bool = True
    version: Optional[str] = None
    checksum: Optional[str] = None

    def __post_init__(self) -> None:
        if self.event_type not in HOUSEHOLD_EVENT_TYPES:
            raise HouseholdTimelineError(f"Unknown household timeline event type: {self.event_type}")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise HouseholdTimelineError("Timeline event end date must be on or after its start date.")
        created = self.created_at or now_iso()
        updated = self.updated_at or created
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
        checksum = self.checksum or self.calculate_checksum()
        object.__setattr__(self, "checksum", checksum)

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        title: str,
        effective_from: date,
        effective_to: Optional[date] = None,
        source: str = "manual",
        confidence: str = "medium",
        notes: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        active: bool = True,
        version: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> "HouseholdTimelineEvent":
        return cls(
            event_id=event_id or f"evt_{uuid.uuid4().hex[:16]}",
            event_type=event_type,
            title=title,
            effective_from=effective_from,
            effective_to=effective_to,
            source=source,
            confidence=confidence,
            notes=notes,
            metadata=metadata or {},
            active=active,
            version=version,
        )

    @classmethod
    def tariff_period(
        cls,
        tariff: TariffVersion,
        *,
        effective_from: date,
        effective_to: Optional[date] = None,
        source: str = "manual",
        confidence: str = "high",
        notes: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> "HouseholdTimelineEvent":
        snapshot = tariff.as_dict()
        snapshot["effective_from"] = effective_from.isoformat()
        snapshot["effective_to"] = effective_to.isoformat() if effective_to else None
        return cls.create(
            event_id=event_id,
            event_type="tariff_period",
            title=tariff.tariff_name,
            effective_from=effective_from,
            effective_to=effective_to,
            source=source,
            confidence=confidence,
            notes=notes,
            metadata={"tariff": snapshot},
            version=tariff.version,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HouseholdTimelineEvent":
        effective_to = data.get("effective_to")
        return cls(
            event_id=str(data["event_id"]),
            event_type=str(data["event_type"]),
            title=str(data["title"]),
            effective_from=date.fromisoformat(str(data["effective_from"])),
            effective_to=date.fromisoformat(str(effective_to)) if effective_to else None,
            source=str(data.get("source") or "manual"),
            confidence=str(data.get("confidence") or "medium"),
            notes=data.get("notes"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            metadata=dict(data.get("metadata") or {}),
            active=bool(data.get("active", True)),
            version=data.get("version"),
            checksum=data.get("checksum"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "title": self.title,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "source": self.source,
            "confidence": self.confidence,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
            "active": self.active,
            "version": self.version,
            "checksum": self.checksum,
        }

    def public_dict(self) -> dict[str, Any]:
        data = self.as_dict()
        tariff = data.get("metadata", {}).get("tariff")
        if isinstance(tariff, dict):
            data["metadata"] = {
                "tariff": {
                    "provider": tariff.get("provider"),
                    "tariff_name": tariff.get("tariff_name"),
                    "version": tariff.get("version"),
                    "version_id": tariff.get("version_id"),
                    "effective_from": tariff.get("effective_from"),
                    "effective_to": tariff.get("effective_to"),
                    "source_identifier": tariff.get("source_identifier"),
                }
            }
        return data

    def calculate_checksum(self) -> str:
        payload = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "title": self.title,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "source": self.source,
            "confidence": self.confidence,
            "notes": self.notes,
            "metadata": self.metadata,
            "active": self.active,
            "version": self.version,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def active_on(self, when: date) -> bool:
        return self.active and self.effective_from <= when and (self.effective_to is None or when <= self.effective_to)

    def tariff_version(self) -> TariffVersion:
        tariff = self.metadata.get("tariff")
        if not isinstance(tariff, dict):
            raise HouseholdTimelineError("Timeline tariff period is missing tariff rates.")
        return TariffVersion.from_dict(tariff)

    def with_updates(self, **updates: Any) -> "HouseholdTimelineEvent":
        data = self.as_dict()
        data.update(updates)
        data["updated_at"] = now_iso()
        data["checksum"] = None
        if isinstance(data.get("effective_from"), date):
            data["effective_from"] = data["effective_from"].isoformat()
        if isinstance(data.get("effective_to"), date):
            data["effective_to"] = data["effective_to"].isoformat()
        return HouseholdTimelineEvent.from_dict(data)


@dataclass(frozen=True)
class TariffResolution:
    tariff: TariffVersion
    source: str
    event_id: Optional[str] = None
    warning: Optional[str] = None

    def public_dict(self) -> dict[str, Any]:
        data = self.tariff.public_metadata()
        data.update({"source": self.source, "event_id": self.event_id, "warning": self.warning})
        return data


@dataclass
class HouseholdTimeline:
    events: list[HouseholdTimelineEvent] = field(default_factory=list)
    schema_version: int = 1

    def __post_init__(self) -> None:
        self.events = sorted(list(self.events), key=lambda item: (item.effective_from, item.created_at))
        self.validate()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HouseholdTimeline":
        return cls(
            schema_version=int(data.get("schema_version") or 1),
            events=[HouseholdTimelineEvent.from_dict(item) for item in data.get("events", [])],
        )

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "events": [event.as_dict() for event in self.events]}

    def public_dict(self, *, today: Optional[date] = None) -> dict[str, Any]:
        current_day = today or date.today()
        tariff_periods = self.tariff_periods()
        active = self.active_tariff_period(current_day)
        return {
            "events": [event.public_dict() for event in self.events if event.active],
            "active_tariff_period": active.public_dict() if active else None,
            "tariff_periods": [event.public_dict() for event in tariff_periods],
            "gaps": [gap_as_dict(gap) for gap in self.tariff_gaps()],
            "warnings": self.warnings(),
        }

    def tariff_periods(self, active_only: bool = True) -> list[HouseholdTimelineEvent]:
        periods = [event for event in self.events if event.event_type == "tariff_period"]
        if active_only:
            periods = [event for event in periods if event.active]
        return sorted(periods, key=lambda event: event.effective_from)

    def active_tariff_period(self, when: date) -> Optional[HouseholdTimelineEvent]:
        matches = [event for event in self.tariff_periods() if event.active_on(when)]
        return matches[-1] if matches else None

    def validate(self) -> None:
        seen: set[tuple[str, date, Optional[date], str]] = set()
        previous: Optional[HouseholdTimelineEvent] = None
        for event in self.tariff_periods():
            event.tariff_version()
            key = (event.title.lower(), event.effective_from, event.effective_to, str(event.version or ""))
            if key in seen:
                raise HouseholdTimelineError("Duplicate tariff period.")
            seen.add(key)
            if previous and periods_overlap(previous, event):
                if previous.effective_to is None:
                    raise HouseholdTimelineError("Open-ended tariff period cannot be followed by another period.")
                raise HouseholdTimelineError("Tariff periods cannot overlap.")
            previous = event

    def add_event(self, event: HouseholdTimelineEvent) -> "HouseholdTimeline":
        return HouseholdTimeline(schema_version=self.schema_version, events=[*self.events, event])

    def replace_event(self, event_id: str, replacement: HouseholdTimelineEvent) -> "HouseholdTimeline":
        if not any(event.event_id == event_id for event in self.events):
            raise HouseholdTimelineError("Timeline event not found.")
        return HouseholdTimeline(
            schema_version=self.schema_version,
            events=[replacement if event.event_id == event_id else event for event in self.events],
        )

    def disable_event(self, event_id: str) -> "HouseholdTimeline":
        event = self.get_event(event_id)
        return self.replace_event(event_id, event.with_updates(active=False))

    def delete_event(self, event_id: str) -> "HouseholdTimeline":
        if not any(event.event_id == event_id for event in self.events):
            raise HouseholdTimelineError("Timeline event not found.")
        return HouseholdTimeline(schema_version=self.schema_version, events=[event for event in self.events if event.event_id != event_id])

    def get_event(self, event_id: str) -> HouseholdTimelineEvent:
        for event in self.events:
            if event.event_id == event_id:
                return event
        raise HouseholdTimelineError("Timeline event not found.")

    def tariff_gaps(self) -> list[tuple[date, Optional[date]]]:
        periods = self.tariff_periods()
        gaps: list[tuple[date, Optional[date]]] = []
        previous: Optional[HouseholdTimelineEvent] = None
        for event in periods:
            if previous and previous.effective_to and previous.effective_to + timedelta(days=1) < event.effective_from:
                gaps.append((previous.effective_to + timedelta(days=1), event.effective_from - timedelta(days=1)))
            previous = event
        if periods and periods[0].source.startswith("inferred"):
            gaps.append((date.min, periods[0].effective_from - timedelta(days=1)))
        return gaps

    def warnings(self) -> list[str]:
        warnings = []
        if not self.tariff_periods():
            warnings.append("No household tariff periods are configured.")
        for start, end in self.tariff_gaps():
            if start == date.min:
                warnings.append(f"Tariff history before {end.isoformat()} is unknown.")
            else:
                warnings.append(f"Tariff history is missing from {start.isoformat()} to {end.isoformat()}.")
        return warnings


class HouseholdTariffResolver:
    """Tariff resolver with household timeline precedence."""

    def __init__(self, timeline: HouseholdTimeline, fallback_resolver: TariffResolver, settings: Any = None) -> None:
        self.timeline = timeline
        self.fallback_resolver = fallback_resolver
        self.settings = settings
        self.store = fallback_resolver.store

    def resolve(self, when: date, *, provider: Optional[str] = None, tariff_name: Optional[str] = None) -> TariffVersion:
        return self.resolve_with_context(when, provider=provider, tariff_name=tariff_name).tariff

    def resolve_with_context(
        self,
        when: date,
        *,
        provider: Optional[str] = None,
        tariff_name: Optional[str] = None,
    ) -> TariffResolution:
        period = self.timeline.active_tariff_period(when)
        if period is not None:
            return TariffResolution(period.tariff_version(), "household_timeline", period.event_id)
        warning = "No household tariff period covers this date; using tariff version fallback."
        try:
            tariff = self.fallback_resolver.resolve(when, provider=provider, tariff_name=tariff_name)
            return TariffResolution(tariff, "tariff_version", warning=warning)
        except TariffVersionError:
            tariff = TariffVersion.from_settings(self.settings, source_identifier="manual_fallback")
            return TariffResolution(tariff, "manual_fallback", warning="No historical tariff was known; current manual rates were used.")

    def resolution_summary(self, start: date, end: date) -> dict[str, Any]:
        periods_used: dict[str, dict[str, Any]] = {}
        unresolved = 0
        fallback = 0
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            resolved = self.resolve_with_context(day)
            key = resolved.event_id or resolved.tariff.version_id
            entry = periods_used.setdefault(
                key,
                {
                    "tariff": resolved.tariff.public_metadata(),
                    "source": resolved.source,
                    "event_id": resolved.event_id,
                    "days": 0,
                    "interval_count": 0,
                },
            )
            entry["days"] += 1
            entry["interval_count"] += 96
            if resolved.warning:
                unresolved += 1
            if resolved.source != "household_timeline":
                fallback += 1
        return {
            "periods_used": list(periods_used.values()),
            "interval_counts": {key: value["interval_count"] for key, value in periods_used.items()},
            "unresolved_intervals": unresolved * 96,
            "fallback_intervals": fallback * 96,
            "confidence": "high" if unresolved == 0 else "medium",
            "warnings": self.timeline.warnings(),
        }


class HouseholdTimelineStore:
    def __init__(self, path: Path = DEFAULT_HOUSEHOLD_TIMELINE_PATH, audit_path: Path = DEFAULT_HOUSEHOLD_TIMELINE_AUDIT_PATH) -> None:
        self.path = Path(path)
        self.audit_path = Path(audit_path)

    def load(self, settings: Any = None) -> HouseholdTimeline:
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise HouseholdTimelineError("Household timeline file must contain a JSON object.")
            return HouseholdTimeline.from_dict(payload)
        if settings is None:
            return HouseholdTimeline()
        return migrate_timeline_from_settings(settings)

    def save(self, timeline: HouseholdTimeline, *, action: str = "save", reason: Optional[str] = None, previous: Optional[HouseholdTimeline] = None) -> HouseholdTimeline:
        timeline.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(timeline.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.append_audit(previous or HouseholdTimeline(), timeline, action=action, reason=reason)
        return timeline

    def append_audit(self, previous: HouseholdTimeline, new: HouseholdTimeline, *, action: str, reason: Optional[str] = None) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "changed_at": now_iso(),
            "action": action,
            "reason": reason,
            "previous": previous.as_dict(),
            "new": new.as_dict(),
        }
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def household_timeline_store(path: Path = DEFAULT_HOUSEHOLD_TIMELINE_PATH) -> HouseholdTimelineStore:
    return HouseholdTimelineStore(path)


def load_household_timeline(path: Path = DEFAULT_HOUSEHOLD_TIMELINE_PATH, settings: Any = None) -> HouseholdTimeline:
    return HouseholdTimelineStore(path).load(settings)


def household_tariff_resolver(settings: Any, timeline: Optional[HouseholdTimeline] = None) -> HouseholdTariffResolver:
    return HouseholdTariffResolver(timeline or load_household_timeline(settings=settings), resolver_for_settings(settings), settings)


def migrate_timeline_from_settings(settings: Any) -> HouseholdTimeline:
    resolver = resolver_for_settings(settings)
    current_day = date.today()
    tariff = resolver.resolve(current_day)
    event = HouseholdTimelineEvent.tariff_period(
        tariff,
        effective_from=tariff.effective_from,
        effective_to=None,
        source="inferred_current_settings",
        confidence="medium",
        notes="Created from the current configured tariff because no household tariff timeline exists yet.",
        event_id="current_tariff",
    )
    return HouseholdTimeline(events=[event])


def tariff_version_from_catalog_or_manual(
    *,
    provider: str,
    tariff_name: str,
    version: str,
    effective_from: date,
    effective_to: Optional[date],
    supply_charge_per_day: float,
    usage_rates: dict[str, float],
    export_credit_rate: float,
    tariff_windows: tuple[str, ...] = (),
    source_identifier: str = "manual_timeline",
) -> TariffVersion:
    return TariffVersion.create(
        provider=provider,
        tariff_name=tariff_name,
        version=version,
        effective_from=effective_from,
        effective_to=effective_to,
        supply_charge_per_day=supply_charge_per_day,
        usage_rates=usage_rates,
        export_rates={"standard": export_credit_rate},
        tariff_windows=tariff_windows,
        source_identifier=source_identifier,
        gst_included=True,
    )


def tariff_version_for_plan(plan_id: str, effective_from: date, effective_to: Optional[date]) -> TariffVersion:
    plan = SUPPORTED_PLANS.get(plan_id)
    if not plan or plan_id == MANUAL_TARIFF_ID:
        raise HouseholdTimelineError("Unknown tariff version.")
    return TariffVersion.from_plan(
        plan,
        effective_from=effective_from,
        effective_to=effective_to,
        version=f"{plan_id}-{effective_from.isoformat()}",
        source_identifier="household_timeline_catalog",
    )


def current_tariff_version_from_settings(settings: Any, effective_from: date, effective_to: Optional[date]) -> TariffVersion:
    tariff = TariffVersion.from_settings(settings, effective_from=effective_from, source_identifier="household_timeline_settings")
    return tariff.with_effective_to(effective_to) if effective_to else tariff


def timeline_summary_sentence(summary: dict[str, Any]) -> str:
    periods = summary.get("periods_used") or []
    if len(periods) <= 1:
        return ""
    labels = []
    for item in periods:
        tariff = item.get("tariff") or {}
        name = tariff.get("tariff_name") or "tariff"
        effective_from = tariff.get("effective_from")
        labels.append(f"{name} from {effective_from}" if effective_from else str(name))
    return "Rates changed during this period. Costs were calculated using " + str(len(periods)) + " tariff periods."


def periods_overlap(first: HouseholdTimelineEvent, second: HouseholdTimelineEvent) -> bool:
    first_end = first.effective_to or date.max
    second_end = second.effective_to or date.max
    return first.effective_from <= second_end and second.effective_from <= first_end


def gap_as_dict(gap: tuple[date, Optional[date]]) -> dict[str, Optional[str]]:
    start, end = gap
    return {
        "start": None if start == date.min else start.isoformat(),
        "end": end.isoformat() if end else None,
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def event_type_label(event_type: str) -> str:
    return str(event_type).replace("_", " ").title()


def default_plan_label(settings: Any) -> str:
    return plan_label(str(getattr(settings, "current_plan", DEFAULT_PLAN) or DEFAULT_PLAN))
