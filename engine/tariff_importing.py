#!/usr/bin/env python3
"""Tariff provider import and approval workflow.

Imported tariffs are staged as pending updates. They are not added to the
active TariffVersionStore until a user explicitly approves them.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol

from .financial_engine import FinancialEngine
from .tariff_catalog import SUPPORTED_PLANS
from .tariff_versioning import TariffResolver, TariffVersion, TariffVersionStore

SYNERGY_SOURCE_URL = "https://www.synergy.net.au/Your-home/Energy-plans"
TARIFF_UPDATE_CHECK_INTERVAL_DAYS = 7


class TariffProvider(Protocol):
    def provider_name(self) -> str:
        ...

    def supported_tariffs(self) -> list[str]:
        ...

    def fetch_current_tariffs(self) -> list["ImportedTariff"]:
        ...


@dataclass(frozen=True)
class ImportedTariff:
    provider: str
    tariff_name: str
    effective_from: date
    supply_charge_per_day: float
    usage_rates: dict[str, float]
    export_rates: dict[str, float]
    tariff_windows: tuple[str, ...]
    currency: str = "AUD"
    source_identifier: str = "retailer_import"
    source_url: Optional[str] = None
    retrieved_at: Optional[str] = None
    gst_included: Optional[bool] = None
    checksum: Optional[str] = None

    def to_version(self) -> TariffVersion:
        retrieved = self.retrieved_at or _utc_now()
        version = f"imported-{self.effective_from.isoformat()}-{(self.checksum or self.calculate_checksum())[:12]}"
        return TariffVersion.create(
            provider=self.provider,
            tariff_name=self.tariff_name,
            version=version,
            effective_from=self.effective_from,
            effective_to=None,
            supply_charge_per_day=self.supply_charge_per_day,
            usage_rates=self.usage_rates,
            export_rates=self.export_rates,
            tariff_windows=self.tariff_windows,
            currency=self.currency,
            source_identifier=self.source_identifier,
            source_url=self.source_url,
            created_at=retrieved,
            gst_included=self.gst_included,
        )

    def calculate_checksum(self) -> str:
        payload = {
            "provider": self.provider,
            "tariff_name": self.tariff_name,
            "effective_from": self.effective_from.isoformat(),
            "supply_charge_per_day": self.supply_charge_per_day,
            "usage_rates": self.usage_rates,
            "export_rates": self.export_rates,
            "tariff_windows": list(self.tariff_windows),
            "currency": self.currency,
            "gst_included": self.gst_included,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SynergyTariffProvider:
    """WA Synergy tariff provider backed by a pluggable structured source."""

    provider_id = "synergy"

    def __init__(
        self,
        *,
        source_url: Optional[str] = None,
        source_loader: Optional[Callable[[str], Any]] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.source_url = source_url or os.environ.get("POWMONITOR_SYNERGY_TARIFF_SOURCE") or SYNERGY_SOURCE_URL
        self.source_loader = source_loader
        self.clock = clock

    def provider_name(self) -> str:
        return "Synergy"

    def supported_tariffs(self) -> list[str]:
        return [plan.label for plan in SUPPORTED_PLANS.values() if plan.retailer_id == self.provider_id]

    def fetch_current_tariffs(self) -> list[ImportedTariff]:
        retrieved_at = self.clock().isoformat(timespec="seconds")
        payload = self._load_source()
        records = payload.get("tariffs", payload if isinstance(payload, list) else [])
        imported = []
        for record in records:
            imported.append(self._record_to_imported_tariff(record, retrieved_at))
        return imported

    def _load_source(self) -> Any:
        if self.source_loader is not None:
            return self.source_loader(self.source_url)
        if self.source_url.startswith("file://"):
            return json.loads(Path(self.source_url[7:]).read_text(encoding="utf-8"))
        if self.source_url.endswith(".json"):
            with urllib.request.urlopen(self.source_url, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        return self._bundled_catalog_payload()

    def _bundled_catalog_payload(self) -> dict[str, Any]:
        return {
            "tariffs": [
                {
                    "tariff_name": plan.label,
                    "effective_from": "2026-07-01",
                    "supply_charge_per_day": plan.supply_charge_per_day,
                    "usage_rates": plan.import_rates,
                    "export_rates": {"standard": plan.export_credit_rate},
                    "tariff_windows": list(plan.tariff_windows),
                    "currency": "AUD",
                    "source_identifier": "synergy_bundled_catalog",
                    "source_url": self.source_url,
                    "gst_included": True,
                }
                for plan in SUPPORTED_PLANS.values()
                if plan.retailer_id == self.provider_id
            ]
        }

    def _record_to_imported_tariff(self, record: dict[str, Any], retrieved_at: str) -> ImportedTariff:
        source_url = record.get("source_url") or self.source_url
        imported = ImportedTariff(
            provider=str(record.get("provider") or self.provider_name()),
            tariff_name=str(record["tariff_name"]),
            effective_from=date.fromisoformat(str(record["effective_from"])),
            supply_charge_per_day=float(record["supply_charge_per_day"]),
            usage_rates={str(key): float(value) for key, value in dict(record.get("usage_rates") or {}).items()},
            export_rates={str(key): float(value) for key, value in dict(record.get("export_rates") or {}).items()},
            tariff_windows=tuple(str(item) for item in record.get("tariff_windows") or ()),
            currency=str(record.get("currency") or "AUD"),
            source_identifier=str(record.get("source_identifier") or "synergy_import"),
            source_url=source_url,
            retrieved_at=str(record.get("retrieved_at") or retrieved_at),
            gst_included=record.get("gst_included"),
            checksum=record.get("checksum"),
        )
        checksum = imported.checksum or imported.calculate_checksum()
        return ImportedTariff(**{**imported.__dict__, "checksum": checksum})


class TariffProviderRegistry:
    def __init__(self, providers: Optional[Iterable[TariffProvider]] = None) -> None:
        self._providers: dict[str, TariffProvider] = {}
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: TariffProvider) -> None:
        key = _provider_key(provider.provider_name())
        if key in self._providers:
            raise ValueError(f"Tariff provider already registered: {provider.provider_name()}")
        self._providers[key] = provider

    def provider(self, provider_name: str) -> TariffProvider:
        return self._providers[_provider_key(provider_name)]

    def providers(self) -> list[TariffProvider]:
        return list(self._providers.values())


class TariffUpdateRepository:
    def __init__(self, path: Optional[Path] = None, state: Optional[dict[str, Any]] = None) -> None:
        self.path = path
        self._state = state

    def load(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        return {
            "pending": [],
            "archived": [],
            "approved_versions": [],
            "diagnostics": {},
        }

    def save(self, state: dict[str, Any]) -> None:
        state.setdefault("pending", [])
        state.setdefault("archived", [])
        state.setdefault("approved_versions", [])
        state.setdefault("diagnostics", {})
        if self._state is not None:
            state = dict(state)
            self._state.clear()
            self._state.update(state)
            return
        if not self.path:
            self._state = state
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def pending_updates(self) -> list[dict[str, Any]]:
        return list(self.load().get("pending") or [])

    def approved_version_dicts(self) -> list[dict[str, Any]]:
        return list(self.load().get("approved_versions") or [])

    def diagnostics(self) -> dict[str, Any]:
        return dict(self.load().get("diagnostics") or {})

    def stage_pending(self, update: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        pending = [
            item for item in state.get("pending", [])
            if item.get("update_id") != update.get("update_id")
        ]
        pending.append(update)
        state["pending"] = pending
        self.save(state)
        return update

    def record_diagnostics(self, provider_name: str, diagnostics: dict[str, Any]) -> None:
        state = self.load()
        all_diagnostics = dict(state.get("diagnostics") or {})
        all_diagnostics[_provider_key(provider_name)] = diagnostics
        state["diagnostics"] = all_diagnostics
        self.save(state)

    def approve(self, update_id: str, approved_at: Optional[str] = None) -> dict[str, Any]:
        state = self.load()
        pending = list(state.get("pending") or [])
        update = next((item for item in pending if item.get("update_id") == update_id), None)
        if update is None:
            raise ValueError(f"Pending tariff update not found: {update_id}")
        state["pending"] = [item for item in pending if item.get("update_id") != update_id]
        state.setdefault("approved_versions", []).append(update["pending_version"])
        archived = {**update, "status": "approved", "resolved_at": approved_at or _utc_now()}
        state.setdefault("archived", []).append(archived)
        self.save(state)
        return archived

    def dismiss(self, update_id: str, dismissed_at: Optional[str] = None) -> dict[str, Any]:
        state = self.load()
        pending = list(state.get("pending") or [])
        update = next((item for item in pending if item.get("update_id") == update_id), None)
        if update is None:
            raise ValueError(f"Pending tariff update not found: {update_id}")
        state["pending"] = [item for item in pending if item.get("update_id") != update_id]
        archived = {**update, "status": "dismissed", "resolved_at": dismissed_at or _utc_now()}
        state.setdefault("archived", []).append(archived)
        self.save(state)
        return archived


class TariffImportManager:
    def __init__(
        self,
        *,
        registry: TariffProviderRegistry,
        active_store: TariffVersionStore,
        repository: TariffUpdateRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.registry = registry
        self.active_store = active_store
        self.repository = repository
        self.clock = clock

    def check_for_updates(self, provider_name: str, usage_profile: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        provider = self.registry.provider(provider_name)
        fetched_at = self.clock().isoformat(timespec="seconds")
        status = "no_change"
        staged_updates = []
        last_error = None
        source_url = None
        try:
            imported = provider.fetch_current_tariffs()
            resolver = TariffResolver(self.active_store)
            for tariff in imported:
                source_url = tariff.source_url or source_url
                pending_version = tariff.to_version()
                active_version = resolver.resolve(
                    tariff.effective_from,
                    provider=tariff.provider,
                    tariff_name=tariff.tariff_name,
                )
                diff = tariff_version_diff(active_version, pending_version)
                if not diff:
                    continue
                update = pending_update_payload(
                    active_version,
                    pending_version,
                    tariff.checksum or tariff_content_checksum(pending_version),
                    diff,
                    usage_profile=usage_profile,
                    detected_at=fetched_at,
                )
                self.repository.stage_pending(update)
                staged_updates.append(update)
            if staged_updates:
                status = "pending_review"
        except Exception as exc:  # pragma: no cover - diagnostics path
            status = "error"
            last_error = str(exc)
        diagnostics = {
            "last_successful_check": fetched_at if status != "error" else None,
            "provider": provider.provider_name(),
            "tariff_version": staged_updates[0]["pending_version"]["version"] if staged_updates else None,
            "current_checksum": staged_updates[0]["current_checksum"] if staged_updates else None,
            "pending_checksum": staged_updates[0]["pending_checksum"] if staged_updates else None,
            "import_status": status,
            "last_error": last_error,
            "source_url": source_url,
            "next_scheduled_check": (self.clock().date() + timedelta(days=TARIFF_UPDATE_CHECK_INTERVAL_DAYS)).isoformat(),
        }
        self.repository.record_diagnostics(provider.provider_name(), diagnostics)
        return {"status": status, "updates": staged_updates, "diagnostics": diagnostics}


def pending_update_payload(
    active_version: TariffVersion,
    pending_version: TariffVersion,
    pending_checksum: str,
    diff: list[dict[str, str]],
    *,
    usage_profile: Optional[dict[str, Any]],
    detected_at: str,
) -> dict[str, Any]:
    current_checksum = tariff_content_checksum(active_version)
    update_id = hashlib.sha256(
        f"{active_version.version_id}:{pending_version.version_id}:{pending_checksum}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "update_id": update_id,
        "status": "pending",
        "provider": pending_version.provider,
        "tariff_name": pending_version.tariff_name,
        "date_detected": detected_at,
        "effective_date": pending_version.effective_from.isoformat(),
        "current_version": active_version.as_dict(),
        "pending_version": pending_version.as_dict(),
        "current_checksum": current_checksum,
        "pending_checksum": pending_checksum,
        "diff": diff,
        "financial_preview": financial_impact_preview(active_version, pending_version, usage_profile or {}),
    }


def tariff_version_diff(current: TariffVersion, pending: TariffVersion) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    if abs(current.supply_charge_per_day - pending.supply_charge_per_day) > 1e-9:
        changes.append(_change("supply_charge", "Supply charge", _money(current.supply_charge_per_day), _money(pending.supply_charge_per_day)))
    current_rates = current.usage_rate_map
    pending_rates = pending.usage_rate_map
    for bucket in sorted(set(current_rates) | set(pending_rates)):
        before = current_rates.get(bucket, 0.0)
        after = pending_rates.get(bucket, 0.0)
        if abs(before - after) > 1e-9:
            changes.append(_change(f"usage:{bucket}", bucket.replace("_", " ").title(), _cents(before), _cents(after)))
    current_exports = current.export_rate_map
    pending_exports = pending.export_rate_map
    for bucket in sorted(set(current_exports) | set(pending_exports)):
        before = current_exports.get(bucket, 0.0)
        after = pending_exports.get(bucket, 0.0)
        if abs(before - after) > 1e-9:
            changes.append(_change(f"export:{bucket}", "Export" if bucket == "standard" else f"Export {bucket}", _cents(before), _cents(after)))
    if tuple(current.tariff_windows) != tuple(pending.tariff_windows):
        changes.append(_change("tariff_windows", "Tariff windows", "; ".join(current.tariff_windows), "; ".join(pending.tariff_windows)))
    if current.gst_included != pending.gst_included:
        changes.append(_change("gst_status", "GST status", _gst(current.gst_included), _gst(pending.gst_included)))
    if current.currency != pending.currency:
        changes.append(_change("currency", "Currency", current.currency, pending.currency))
    current_checksum = tariff_content_checksum(current)
    pending_checksum = tariff_content_checksum(pending)
    if not changes and current_checksum != pending_checksum:
        changes.append(_change("checksum", "Checksum", current_checksum[:12], pending_checksum[:12]))
    return changes


def tariff_content_checksum(tariff: TariffVersion) -> str:
    payload = {
        "provider": tariff.provider,
        "tariff_name": tariff.tariff_name,
        "effective_from": tariff.effective_from.isoformat(),
        "supply_charge_per_day": tariff.supply_charge_per_day,
        "usage_rates": tariff.usage_rate_map,
        "export_rates": tariff.export_rate_map,
        "tariff_windows": list(tariff.tariff_windows),
        "currency": tariff.currency,
        "gst_included": tariff.gst_included,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def financial_impact_preview(current: TariffVersion, pending: TariffVersion, usage_profile: dict[str, Any]) -> dict[str, Any]:
    days = int(usage_profile.get("supply_days") or usage_profile.get("modelled_days") or 0)
    imports = dict(usage_profile.get("grid_import_by_bucket") or {})
    export_kwh = float(usage_profile.get("solar_exported_kwh") or 0.0)
    if days <= 0:
        return {
            "available": False,
            "basis": "Recent household usage is unavailable.",
            "annual_bill_difference": None,
            "estimated_change": "unknown",
            "battery_recommendation_impact": "Battery recommendation impact is unavailable until recent usage exists.",
            "plan_recommendation_impact": "Plan recommendation impact is unavailable until recent usage exists.",
        }
    current_cost = _bill_for_usage(current, days, imports, export_kwh)
    pending_cost = _bill_for_usage(pending, days, imports, export_kwh)
    annual_delta = FinancialEngine().projected_annual_cost(pending_cost - current_cost, days)
    spread_before = max(current.usage_rate_map.values() or [0.0]) - min(current.usage_rate_map.values() or [0.0])
    spread_after = max(pending.usage_rate_map.values() or [0.0]) - min(pending.usage_rate_map.values() or [0.0])
    return {
        "available": True,
        "basis": f"Annualised from {days} days of recent household usage.",
        "annual_bill_difference": annual_delta,
        "estimated_change": "increase" if annual_delta > 0 else "decrease" if annual_delta < 0 else "no_change",
        "summary": (
            f"Using the previous {days} days of usage, this tariff would "
            f"{'increase' if annual_delta > 0 else 'decrease' if annual_delta < 0 else 'not materially change'} "
            f"annual electricity costs by approximately ${abs(annual_delta):.0f}."
        ),
        "battery_recommendation_impact": (
            "Battery tariff-arbitrage value may increase because the spread between cheapest and highest import rates increases."
            if spread_after > spread_before + 1e-9
            else "Battery tariff-arbitrage value may decrease because the spread between cheapest and highest import rates narrows."
            if spread_after < spread_before - 1e-9
            else "Battery recommendation impact is expected to be minimal because the tariff spread is unchanged."
        ),
        "plan_recommendation_impact": "Plan comparisons should be reviewed before approval because import/export values changed.",
    }


def usage_profile_from_summary(summary: Any) -> dict[str, Any]:
    buckets = getattr(summary, "buckets", {}) or {}
    return {
        "supply_days": int(getattr(summary, "supply_days", 0) or 0),
        "grid_import_by_bucket": {
            bucket: float(values.get("kwh") or 0.0)
            for bucket, values in buckets.items()
        },
        "solar_exported_kwh": float(getattr(summary, "solar_exported_kwh", 0.0) or 0.0),
    }


def default_tariff_provider_registry() -> TariffProviderRegistry:
    return TariffProviderRegistry([SynergyTariffProvider()])


def _bill_for_usage(tariff: TariffVersion, days: int, imports: dict[str, float], export_kwh: float) -> float:
    import_cost = sum(float(kwh or 0.0) * tariff.usage_rate(str(bucket)) for bucket, kwh in imports.items())
    supply = days * tariff.supply_charge_per_day
    export_credit = export_kwh * tariff.primary_export_rate
    return import_cost + supply - export_credit


def _change(field: str, label: str, before: str, after: str) -> dict[str, str]:
    return {
        "field": field,
        "label": label,
        "before": before,
        "after": after,
        "summary": f"{label}: {before} -> {after}",
    }


def _provider_key(value: str) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _money(value: float) -> str:
    return f"${float(value):.2f}"


def _cents(value: float) -> str:
    return f"{float(value) * 100:.1f} c"


def _gst(value: Optional[bool]) -> str:
    if value is None:
        return "Unknown"
    return "GST included" if value else "GST excluded"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
