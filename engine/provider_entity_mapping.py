#!/usr/bin/env python3
"""Reusable entity selection and mapping support for provider integrations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .provider_framework import ProviderCapability, ProviderObservationType


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROVIDER_MAPPING_PATH = PROJECT_ROOT / "config" / "provider_mappings.json"


class ProviderEntityCategory(str, Enum):
    GRID = "grid"
    SOLAR = "solar"
    HOME_CONSUMPTION = "home_consumption"
    BATTERY = "battery"
    EV_CHARGER = "ev_charger"
    EV_STATUS = "ev_status"
    ENERGY = "energy"
    OTHER = "other"


@dataclass(frozen=True)
class ProviderMappingTarget:
    target_id: str
    label: str
    category: ProviderEntityCategory
    capability: ProviderCapability
    observation_type: ProviderObservationType
    compatible_units: set[str] = field(default_factory=set)
    required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "label": self.label,
            "category": self.category.value,
            "capability": self.capability.value,
            "observation_type": self.observation_type.value,
            "compatible_units": sorted(self.compatible_units),
            "required": self.required,
        }


@dataclass(frozen=True)
class ProviderEntityOption:
    entity_id: str
    name: str
    category: ProviderEntityCategory
    state: Optional[str] = None
    unit: Optional[str] = None
    device_class: Optional[str] = None
    last_updated: Optional[str] = None
    available: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "category": self.category.value,
            "state": self.state,
            "unit": self.unit,
            "device_class": self.device_class,
            "last_updated": self.last_updated,
            "available": self.available,
        }


@dataclass(frozen=True)
class ProviderMappingValidation:
    status: str
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    target_status: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "target_status": dict(self.target_status),
        }


@dataclass(frozen=True)
class ProviderEntityMappingPlan:
    provider_id: str
    categories: dict[str, list[ProviderEntityOption]]
    targets: list[ProviderMappingTarget]
    recommended_mappings: dict[str, str]
    selected_mappings: dict[str, str]
    validation: ProviderMappingValidation
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    updated_at: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "categories": {
                category: [option.as_dict() for option in options]
                for category, options in self.categories.items()
            },
            "targets": [target.as_dict() for target in self.targets],
            "recommended_mappings": dict(self.recommended_mappings),
            "selected_mappings": dict(self.selected_mappings),
            "validation": self.validation.as_dict(),
            "diagnostics": [dict(item) for item in self.diagnostics],
            "updated_at": self.updated_at,
        }


POWER_UNITS = {"w", "kw"}
ENERGY_UNITS = {"wh", "kwh"}
PERCENT_UNITS = {"%", "percent"}
BOOLEAN_UNITS = {""}

HOME_ASSISTANT_MAPPING_TARGETS = [
    ProviderMappingTarget("grid_live_power", "Grid import/export power", ProviderEntityCategory.GRID, ProviderCapability.LIVE_POWER, ProviderObservationType.GRID_LIVE_POWER, POWER_UNITS),
    ProviderMappingTarget("solar_live_power", "Solar production power", ProviderEntityCategory.SOLAR, ProviderCapability.DAILY_SUMMARY, ProviderObservationType.SOLAR_LIVE_POWER, POWER_UNITS),
    ProviderMappingTarget("home_live_power", "Home consumption power", ProviderEntityCategory.HOME_CONSUMPTION, ProviderCapability.LIVE_POWER, ProviderObservationType.HOME_LIVE_POWER, POWER_UNITS),
    ProviderMappingTarget("battery_soc", "Battery state of charge", ProviderEntityCategory.BATTERY, ProviderCapability.BATTERY_STATE, ProviderObservationType.BATTERY_SOC, PERCENT_UNITS),
    ProviderMappingTarget("battery_power", "Battery charge/discharge power", ProviderEntityCategory.BATTERY, ProviderCapability.BATTERY_STATE, ProviderObservationType.BATTERY_POWER, POWER_UNITS),
    ProviderMappingTarget("ev_charger_live_power", "EV charging power", ProviderEntityCategory.EV_CHARGER, ProviderCapability.EV_SESSIONS, ProviderObservationType.EV_CHARGER_LIVE_POWER, POWER_UNITS),
    ProviderMappingTarget("ev_connected_state", "EV connected state", ProviderEntityCategory.EV_STATUS, ProviderCapability.EV_SESSIONS, ProviderObservationType.EV_CONNECTED_STATE, BOOLEAN_UNITS),
    ProviderMappingTarget("ev_charging_state", "EV charging state", ProviderEntityCategory.EV_STATUS, ProviderCapability.EV_SESSIONS, ProviderObservationType.EV_CHARGING_STATE, BOOLEAN_UNITS),
    ProviderMappingTarget("grid_import_energy", "Grid import energy", ProviderEntityCategory.ENERGY, ProviderCapability.INTERVAL_HISTORY, ProviderObservationType.GRID_IMPORT, ENERGY_UNITS),
    ProviderMappingTarget("solar_generation_energy", "Solar generation energy", ProviderEntityCategory.ENERGY, ProviderCapability.DAILY_SUMMARY, ProviderObservationType.SOLAR_GENERATION, ENERGY_UNITS),
]


def build_home_assistant_entity_mapping_plan(
    entities: list[dict[str, Any]],
    selected_mappings: Optional[dict[str, str]] = None,
    *,
    updated_at: Optional[str] = None,
) -> ProviderEntityMappingPlan:
    options = [home_assistant_entity_option(entity) for entity in entities]
    categories = {category.value: [] for category in ProviderEntityCategory}
    for option in options:
        categories[option.category.value].append(option)
    recommendations = recommend_entity_mappings(options, HOME_ASSISTANT_MAPPING_TARGETS)
    selected = clean_entity_mappings(selected_mappings) if selected_mappings else dict(recommendations)
    validation = validate_entity_mappings(selected, options, HOME_ASSISTANT_MAPPING_TARGETS)
    diagnostics = entity_mapping_diagnostics(selected, options, HOME_ASSISTANT_MAPPING_TARGETS, validation)
    return ProviderEntityMappingPlan(
        provider_id="home_assistant",
        categories=categories,
        targets=HOME_ASSISTANT_MAPPING_TARGETS,
        recommended_mappings=recommendations,
        selected_mappings=selected,
        validation=validation,
        diagnostics=diagnostics,
        updated_at=updated_at,
    )


def home_assistant_entity_option(entity: dict[str, Any]) -> ProviderEntityOption:
    attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    entity_id = str(entity.get("entity_id") or "")
    state = str(entity.get("state") or "")
    unit = str(attributes.get("unit_of_measurement") or "")
    return ProviderEntityOption(
        entity_id=entity_id,
        name=str(attributes.get("friendly_name") or entity_id),
        category=categorize_home_assistant_entity(entity),
        state=state,
        unit=unit or None,
        device_class=str(attributes.get("device_class") or "") or None,
        last_updated=str(entity.get("last_updated") or entity.get("last_changed") or "") or None,
        available=state.strip().lower() not in {"unknown", "unavailable", "none", ""},
    )


def categorize_home_assistant_entity(entity: dict[str, Any]) -> ProviderEntityCategory:
    attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    entity_id = str(entity.get("entity_id") or "").lower()
    friendly = str(attributes.get("friendly_name") or entity_id).lower()
    unit = str(attributes.get("unit_of_measurement") or "").lower()
    device_class = str(attributes.get("device_class") or "").lower()
    haystack = f"{entity_id} {friendly}"
    if any(word in haystack for word in ("ev", "charger", "ocpp")):
        if not unit and any(word in haystack for word in ("connected", "plug", "connector", "charging", "status")):
            return ProviderEntityCategory.EV_STATUS
        return ProviderEntityCategory.EV_CHARGER
    if any(word in haystack for word in ("battery", "soc", "state of charge")) or device_class == "battery":
        return ProviderEntityCategory.BATTERY
    if any(word in haystack for word in ("solar", "pv", "production", "inverter")):
        return ProviderEntityCategory.SOLAR
    if any(word in haystack for word in ("grid", "import", "export", "meter")):
        return ProviderEntityCategory.GRID
    if any(word in haystack for word in ("home", "house", "load", "consumption")):
        return ProviderEntityCategory.HOME_CONSUMPTION
    if device_class == "energy" or unit in ENERGY_UNITS:
        return ProviderEntityCategory.ENERGY
    return ProviderEntityCategory.OTHER


def recommend_entity_mappings(options: list[ProviderEntityOption], targets: list[ProviderMappingTarget]) -> dict[str, str]:
    recommendations: dict[str, str] = {}
    used: set[str] = set()
    for target in targets:
        scored = sorted(
            (
                (_entity_score_for_target(option, target), option.entity_id)
                for option in options
                if option.entity_id not in used
                and option.category == target.category
                and _unit_compatible(option.unit, target.compatible_units)
            ),
            reverse=True,
        )
        best = next((entity_id for score, entity_id in scored if score > 0), None)
        if best:
            recommendations[target.target_id] = best
            used.add(best)
    return recommendations


def validate_entity_mappings(
    selected_mappings: dict[str, str],
    options: list[ProviderEntityOption],
    targets: list[ProviderMappingTarget],
) -> ProviderMappingValidation:
    by_entity = {option.entity_id: option for option in options}
    by_target = {target.target_id: target for target in targets}
    warnings: list[str] = []
    errors: list[str] = []
    target_status: dict[str, str] = {}
    assigned: dict[str, list[str]] = {}
    for target_id, entity_id in selected_mappings.items():
        target = by_target.get(target_id)
        if not target:
            warnings.append(f"{target_id} is not a supported mapping target.")
            target_status[target_id] = "unsupported"
            continue
        option = by_entity.get(entity_id)
        if not option:
            warnings.append(f"{target.label} is mapped to {entity_id}, but that entity was not discovered.")
            target_status[target_id] = "missing"
            continue
        assigned.setdefault(entity_id, []).append(target_id)
        if not option.available:
            warnings.append(f"{target.label} is mapped to {entity_id}, but the entity is unavailable.")
            target_status[target_id] = "unavailable"
            continue
        if not _unit_compatible(option.unit, target.compatible_units):
            warnings.append(f"{target.label} expects {', '.join(sorted(target.compatible_units)) or 'no unit'}, but {entity_id} reports {option.unit or 'no unit'}.")
            target_status[target_id] = "invalid_unit"
            continue
        target_status[target_id] = "configured"
    for target in targets:
        if target.required and target.target_id not in selected_mappings:
            warnings.append(f"{target.label} is not mapped yet.")
            target_status[target.target_id] = "missing"
    for entity_id, target_ids in assigned.items():
        if len(target_ids) > 1:
            warnings.append(f"{entity_id} is assigned to multiple mappings: {', '.join(sorted(target_ids))}.")
            for target_id in target_ids:
                target_status[target_id] = "duplicate"
    status = "error" if errors else "warning" if warnings else "ok"
    return ProviderMappingValidation(status=status, warnings=warnings, errors=errors, target_status=target_status)


def entity_mapping_diagnostics(
    selected_mappings: dict[str, str],
    options: list[ProviderEntityOption],
    targets: list[ProviderMappingTarget],
    validation: ProviderMappingValidation,
) -> list[dict[str, Any]]:
    by_entity = {option.entity_id: option for option in options}
    by_target = {target.target_id: target for target in targets}
    rows = []
    for target_id, entity_id in selected_mappings.items():
        target = by_target.get(target_id)
        option = by_entity.get(entity_id)
        rows.append(
            {
                "target_id": target_id,
                "target_label": target.label if target else target_id,
                "selected_entity": entity_id,
                "entity_state": option.state if option else None,
                "last_update_time": option.last_updated if option else None,
                "unit": option.unit if option else None,
                "availability": "available" if option and option.available else "missing" if option is None else "unavailable",
                "last_successful_observation": None,
                "health": _entity_mapping_health(validation.target_status.get(target_id, "configured"), option),
            }
        )
    return rows


def clean_entity_mappings(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(target_id): str(entity_id)
        for target_id, entity_id in value.items()
        if str(target_id).strip() and str(entity_id).strip()
    }


def load_provider_entity_mappings(
    provider_id: str,
    *,
    path: Path = DEFAULT_PROVIDER_MAPPING_PATH,
) -> dict[str, str]:
    data = _load_mapping_file(path)
    provider = data.get(provider_id) if isinstance(data, dict) else None
    if isinstance(provider, dict):
        return clean_entity_mappings(provider.get("selected_mappings") or provider.get("mappings"))
    return {}


def load_provider_mapping_record(
    provider_id: str,
    *,
    path: Path = DEFAULT_PROVIDER_MAPPING_PATH,
) -> dict[str, Any]:
    data = _load_mapping_file(path)
    provider = data.get(provider_id) if isinstance(data, dict) else None
    return provider if isinstance(provider, dict) else {}


def save_provider_entity_mappings(
    provider_id: str,
    mappings: dict[str, str],
    *,
    path: Path = DEFAULT_PROVIDER_MAPPING_PATH,
) -> dict[str, Any]:
    data = _load_mapping_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "selected_mappings": clean_entity_mappings(mappings),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data[provider_id] = record
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def _load_mapping_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _unit_compatible(unit: Optional[str], compatible_units: set[str]) -> bool:
    normalized = str(unit or "").strip().lower()
    return normalized in compatible_units


def _entity_score_for_target(option: ProviderEntityOption, target: ProviderMappingTarget) -> int:
    text = f"{option.entity_id} {option.name}".lower()
    score = 0
    if option.category == target.category:
        score += 40
    keyword_groups = {
        "grid_live_power": ("grid", "import", "export", "meter", "power"),
        "solar_live_power": ("solar", "pv", "production", "power"),
        "home_live_power": ("home", "house", "load", "consumption", "power"),
        "battery_soc": ("battery", "soc", "state of charge"),
        "battery_power": ("battery", "power", "charge", "discharge"),
        "ev_charger_live_power": ("ev", "charger", "ocpp", "power"),
        "ev_connected_state": ("ev", "connected", "plug", "connector"),
        "ev_charging_state": ("ev", "charging", "charge status"),
        "grid_import_energy": ("grid", "import", "energy"),
        "solar_generation_energy": ("solar", "production", "generation", "energy"),
    }
    for keyword in keyword_groups.get(target.target_id, ()):
        if keyword in text:
            score += 12
    if option.available:
        score += 5
    return score


def _entity_mapping_health(status: str, option: Optional[ProviderEntityOption]) -> str:
    if status != "configured":
        return status
    if option is None:
        return "missing"
    if not option.available:
        return "unavailable"
    if _stale_entity(option.last_updated):
        return "stale"
    return "configured"


def _stale_entity(last_updated: Optional[str]) -> bool:
    if not last_updated:
        return False
    try:
        observed = datetime.fromisoformat(str(last_updated).replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
    return age_seconds > 30 * 60
