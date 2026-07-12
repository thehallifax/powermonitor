#!/usr/bin/env python3
"""Versioned tariff models and date-based resolution.

Tariff versions are immutable financial inputs. Importers can add new versions
later without changing financial callers: callers ask the resolver for the
version applicable to a date, then calculate against that version.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from .tariff_catalog import DEFAULT_PLAN, MANUAL_TARIFF_ID, SUPPORTED_PLANS, TariffPlan, plan_label

DEFAULT_TARIFF_EFFECTIVE_FROM = date(2026, 7, 1)
LEGACY_TARIFF_EFFECTIVE_FROM = date(1900, 1, 1)


class TariffVersionError(ValueError):
    """Raised when a tariff version cannot be created or resolved."""


class TariffVersionImmutableError(TariffVersionError):
    """Raised when a caller attempts to replace an existing version identity."""


def _normalise_rates(values: dict[str, float] | tuple[tuple[str, float], ...]) -> tuple[tuple[str, float], ...]:
    items = values.items() if isinstance(values, dict) else values
    return tuple(sorted((str(key), float(value)) for key, value in items))


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_") or "tariff"


@dataclass(frozen=True)
class TariffVersion:
    provider: str
    tariff_name: str
    version: str
    effective_from: date
    effective_to: Optional[date]
    supply_charge_per_day: float
    usage_rates: tuple[tuple[str, float], ...]
    export_rates: tuple[tuple[str, float], ...]
    tariff_windows: tuple[str, ...]
    currency: str
    source_identifier: str
    source_url: Optional[str]
    created_at: str
    version_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", str(self.provider))
        object.__setattr__(self, "tariff_name", str(self.tariff_name))
        object.__setattr__(self, "version", str(self.version))
        object.__setattr__(self, "supply_charge_per_day", float(self.supply_charge_per_day))
        object.__setattr__(self, "usage_rates", _normalise_rates(self.usage_rates))
        object.__setattr__(self, "export_rates", _normalise_rates(self.export_rates))
        object.__setattr__(self, "tariff_windows", tuple(str(window) for window in self.tariff_windows))
        object.__setattr__(self, "currency", str(self.currency or "AUD"))
        object.__setattr__(self, "source_identifier", str(self.source_identifier))
        object.__setattr__(self, "source_url", str(self.source_url) if self.source_url else None)
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise TariffVersionError("effective_to cannot be before effective_from")
        expected = self.calculate_hash()
        if self.version_hash and self.version_hash != expected:
            raise TariffVersionError("version_hash does not match tariff version contents")
        object.__setattr__(self, "version_hash", expected)

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        tariff_name: str,
        version: str,
        effective_from: date,
        effective_to: Optional[date],
        supply_charge_per_day: float,
        usage_rates: dict[str, float] | tuple[tuple[str, float], ...],
        export_rates: dict[str, float] | tuple[tuple[str, float], ...],
        tariff_windows: tuple[str, ...] = (),
        currency: str = "AUD",
        source_identifier: str = "catalog",
        source_url: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> "TariffVersion":
        created = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        draft = cls(
            provider=provider,
            tariff_name=tariff_name,
            version=version,
            effective_from=effective_from,
            effective_to=effective_to,
            supply_charge_per_day=supply_charge_per_day,
            usage_rates=_normalise_rates(usage_rates),
            export_rates=_normalise_rates(export_rates),
            tariff_windows=tuple(tariff_windows),
            currency=currency,
            source_identifier=source_identifier,
            source_url=source_url,
            created_at=created,
            version_hash="",
        )
        return draft

    @classmethod
    def from_plan(
        cls,
        plan: TariffPlan,
        *,
        effective_from: date = DEFAULT_TARIFF_EFFECTIVE_FROM,
        effective_to: Optional[date] = None,
        version: Optional[str] = None,
        source_identifier: str = "static_catalog",
        created_at: str = "2026-07-01T00:00:00+00:00",
    ) -> "TariffVersion":
        return cls.create(
            provider=plan.retailer_label,
            tariff_name=plan.label,
            version=version or effective_from.isoformat(),
            effective_from=effective_from,
            effective_to=effective_to,
            supply_charge_per_day=plan.supply_charge_per_day,
            usage_rates=plan.import_rates,
            export_rates={"standard": plan.export_credit_rate},
            tariff_windows=plan.tariff_windows,
            currency="AUD",
            source_identifier=source_identifier,
            created_at=created_at,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        effective_from: date = DEFAULT_TARIFF_EFFECTIVE_FROM,
        source_identifier: str = "local_settings",
    ) -> "TariffVersion":
        provider = _value(settings, "retailer") or _value(settings, "retailer_id") or "Manual"
        tariff_name = plan_label(str(_value(settings, "current_plan") or MANUAL_TARIFF_ID))
        return cls.create(
            provider=str(provider),
            tariff_name=tariff_name,
            version=f"settings-{effective_from.isoformat()}",
            effective_from=effective_from,
            effective_to=None,
            supply_charge_per_day=float(_value(settings, "supply_charge_per_day", 0.0) or 0.0),
            usage_rates=dict(_value(settings, "import_rates", {}) or {}),
            export_rates={"standard": float(_value(settings, "export_credit_rate", 0.0) or 0.0)},
            tariff_windows=tuple(_value(settings, "tariff_windows", ()) or ()),
            currency="AUD",
            source_identifier=source_identifier,
        )

    @property
    def version_id(self) -> str:
        return f"{_slug(self.provider)}:{_slug(self.tariff_name)}:{_slug(self.version)}:{self.version_hash[:12]}"

    @property
    def usage_rate_map(self) -> dict[str, float]:
        return dict(self.usage_rates)

    @property
    def export_rate_map(self) -> dict[str, float]:
        return dict(self.export_rates)

    @property
    def primary_export_rate(self) -> float:
        rates = self.export_rate_map
        return float(rates.get("standard", next(iter(rates.values()), 0.0)))

    def usage_rate(self, bucket: str) -> float:
        return float(self.usage_rate_map.get(bucket, 0.0))

    def calculate_hash(self) -> str:
        payload = {
            "provider": self.provider,
            "tariff_name": self.tariff_name,
            "version": self.version,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "supply_charge_per_day": float(self.supply_charge_per_day),
            "usage_rates": list(_normalise_rates(self.usage_rates)),
            "export_rates": list(_normalise_rates(self.export_rates)),
            "tariff_windows": list(self.tariff_windows),
            "currency": self.currency,
            "source_identifier": self.source_identifier,
            "source_url": self.source_url,
            "created_at": self.created_at,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def with_effective_to(self, effective_to: date) -> "TariffVersion":
        return replace(self, effective_to=effective_to, version_hash="")

    def applies_on(self, when: date) -> bool:
        return self.effective_from <= when and (self.effective_to is None or when <= self.effective_to)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "provider": self.provider,
            "tariff_name": self.tariff_name,
            "version": self.version,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "supply_charge_per_day": self.supply_charge_per_day,
            "usage_rates": self.usage_rate_map,
            "export_rates": self.export_rate_map,
            "tariff_windows": list(self.tariff_windows),
            "currency": self.currency,
            "source_identifier": self.source_identifier,
            "source_url": self.source_url,
            "created_at": self.created_at,
            "version_hash": self.version_hash,
        }

    def public_metadata(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "tariff_name": self.tariff_name,
            "version": self.version,
            "version_id": self.version_id,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "currency": self.currency,
            "source_identifier": self.source_identifier,
        }


class TariffVersionStore:
    def __init__(self, versions: Optional[list[TariffVersion]] = None) -> None:
        self._versions: list[TariffVersion] = []
        for version in versions or []:
            self.add_version(version)

    def add_version(self, version: TariffVersion) -> TariffVersion:
        if any(existing.version_id == version.version_id for existing in self._versions):
            raise TariffVersionImmutableError(f"Tariff version already exists: {version.version_id}")
        closed_versions: list[TariffVersion] = []
        for existing in self._versions:
            same_tariff = (
                existing.provider.lower() == version.provider.lower()
                and existing.tariff_name.lower() == version.tariff_name.lower()
            )
            if same_tariff and existing.effective_from < version.effective_from and (
                existing.effective_to is None or existing.effective_to >= version.effective_from
            ):
                closed_versions.append(existing.with_effective_to(version.effective_from - timedelta(days=1)))
            else:
                closed_versions.append(existing)
        closed_versions.append(version)
        self._versions = sorted(closed_versions, key=lambda item: (item.provider.lower(), item.tariff_name.lower(), item.effective_from))
        return version

    def versions(self, provider: Optional[str] = None, tariff_name: Optional[str] = None) -> list[TariffVersion]:
        result = list(self._versions)
        if provider is not None:
            result = [item for item in result if item.provider.lower() == provider.lower()]
        if tariff_name is not None:
            result = [item for item in result if item.tariff_name.lower() == tariff_name.lower()]
        return result


class TariffResolver:
    def __init__(
        self,
        store: TariffVersionStore,
        *,
        default_provider: Optional[str] = None,
        default_tariff_name: Optional[str] = None,
    ) -> None:
        self.store = store
        self.default_provider = default_provider
        self.default_tariff_name = default_tariff_name

    def resolve(
        self,
        when: date,
        *,
        provider: Optional[str] = None,
        tariff_name: Optional[str] = None,
    ) -> TariffVersion:
        provider_name = provider or self.default_provider
        plan_name = tariff_name or self.default_tariff_name
        candidates = self.store.versions(provider_name, plan_name)
        active = [version for version in candidates if version.applies_on(when)]
        if active:
            return sorted(active, key=lambda version: version.effective_from)[-1]
        earlier = [version for version in candidates if version.effective_from <= when]
        if earlier:
            return sorted(earlier, key=lambda version: version.effective_from)[-1]
        future = sorted(candidates, key=lambda version: version.effective_from)
        if future:
            return future[0]
        raise TariffVersionError(f"No tariff version available for {provider_name or '*'} / {plan_name or '*'} on {when.isoformat()}")


def default_tariff_store(settings: Optional[Any] = None) -> TariffVersionStore:
    store = TariffVersionStore()
    for plan in SUPPORTED_PLANS.values():
        historical = TariffVersion.from_plan(
            plan,
            effective_from=LEGACY_TARIFF_EFFECTIVE_FROM,
            effective_to=None,
            version="legacy",
            source_identifier="static_catalog_legacy",
        )
        current = TariffVersion.from_plan(plan, effective_from=DEFAULT_TARIFF_EFFECTIVE_FROM)
        store.add_version(historical)
        store.add_version(current)
    if settings is not None and str(_value(settings, "current_plan") or "") == MANUAL_TARIFF_ID:
        store.add_version(TariffVersion.from_settings(settings, effective_from=LEGACY_TARIFF_EFFECTIVE_FROM))
    return store


def resolver_for_settings(settings: Any) -> TariffResolver:
    plan_id = str(_value(settings, "current_plan") or DEFAULT_PLAN)
    provider = str(_value(settings, "retailer") or "Synergy")
    tariff_name = plan_label(plan_id)
    return TariffResolver(default_tariff_store(settings), default_provider=provider, default_tariff_name=tariff_name)


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)
