#!/usr/bin/env python3
"""Persistent household profile independent of measurement providers."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME_PROFILE_PATH = PROJECT_ROOT / "config" / "home_profile.json"
HOME_PROFILE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class HomeProfile:
    """Stable, user-declared description of the home itself."""

    schema_version: int = HOME_PROFILE_SCHEMA_VERSION
    household_id: str = "default_household"
    location_region: str = ""
    electricity_retailer: str = ""
    current_tariff: str = ""
    solar_installed: bool = False
    solar_capacity_kw: Optional[float] = None
    inverter_brand: str = ""
    inverter_model: str = ""
    battery_installed: bool = False
    battery_capacity_kwh: Optional[float] = None
    ev_owned: bool = False
    vehicle_make: str = ""
    vehicle_model: str = ""
    charger_type: str = ""
    household_monitoring_available: bool = False
    home_assistant_available: bool = False
    extra_fields: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def default(cls, settings: Optional[Any] = None) -> "HomeProfile":
        return cls(
            location_region=_settings_value(settings, "state_or_territory", ""),
            electricity_retailer=_settings_value(settings, "retailer", ""),
            current_tariff=_settings_value(settings, "current_plan", ""),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any], settings: Optional[Any] = None) -> "HomeProfile":
        if not isinstance(data, dict):
            raise ValueError("Home profile must be a JSON object")
        defaults = cls.default(settings)
        known = set(cls.__dataclass_fields__) - {"extra_fields"}
        extra = dict(data.get("extra_fields") or {})
        extra.update({key: value for key, value in data.items() if key not in known and key != "extra_fields"})
        return cls(
            schema_version=int(data.get("schema_version", defaults.schema_version)),
            household_id=str(data.get("household_id") or defaults.household_id),
            location_region=str(data.get("location_region") or defaults.location_region),
            electricity_retailer=str(data.get("electricity_retailer") or defaults.electricity_retailer),
            current_tariff=str(data.get("current_tariff") or defaults.current_tariff),
            solar_installed=_bool(data.get("solar_installed", defaults.solar_installed)),
            solar_capacity_kw=_optional_float(data.get("solar_capacity_kw")),
            inverter_brand=str(data.get("inverter_brand") or ""),
            inverter_model=str(data.get("inverter_model") or ""),
            battery_installed=_bool(data.get("battery_installed", defaults.battery_installed)),
            battery_capacity_kwh=_optional_float(data.get("battery_capacity_kwh")),
            ev_owned=_bool(data.get("ev_owned", defaults.ev_owned)),
            vehicle_make=str(data.get("vehicle_make") or ""),
            vehicle_model=str(data.get("vehicle_model") or ""),
            charger_type=str(data.get("charger_type") or ""),
            household_monitoring_available=_bool(
                data.get(
                    "household_monitoring_available",
                    defaults.household_monitoring_available,
                )
            ),
            home_assistant_available=_bool(
                data.get("home_assistant_available", defaults.home_assistant_available)
            ),
            extra_fields=extra,
        )

    def as_dict(self) -> dict[str, Any]:
        data = {**self.extra_fields, **asdict(self)}
        data.pop("extra_fields", None)
        return data


def load_home_profile(
    path: Path = DEFAULT_HOME_PROFILE_PATH,
    settings: Optional[Any] = None,
) -> HomeProfile:
    path = Path(path)
    if not path.exists():
        return HomeProfile.default(settings)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Home profile must be valid JSON: {path}") from exc
    return HomeProfile.from_dict(raw, settings=settings)


def save_home_profile(
    profile: HomeProfile,
    path: Path = DEFAULT_HOME_PROFILE_PATH,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(profile.as_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _settings_value(settings: Optional[Any], name: str, default: str) -> str:
    if settings is None:
        return default
    if isinstance(settings, dict):
        value = settings.get(name, default)
    else:
        value = getattr(settings, name, default)
    return str(value or default)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Home profile numeric value is invalid: {value}") from exc
