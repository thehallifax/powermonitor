#!/usr/bin/env python3
"""Canonical current household facts for PowerMonitor.

Household State is descriptive only. It normalizes current facts from trusted
platform data so Context, Advisors, Briefing, Goals, and UI layers do not each
rebuild their own view of the household.
"""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from .context_engine import current_tariff_bucket, import_rates, next_lower_cost_window, lowest_cost_window


@dataclass(frozen=True)
class HouseholdPowerState:
    importing: bool
    exporting: bool
    import_export_state: str
    grid_import_kwh_today: float = 0.0
    solar_exported_kwh_today: float = 0.0
    current_grid_power_w: Optional[float] = None
    current_home_power_w: Optional[float] = None
    source: str = "daily_summary"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HouseholdEnergyState:
    solar_generated_kwh_today: float = 0.0
    solar_used_kwh_today: float = 0.0
    solar_exported_kwh_today: float = 0.0
    grid_import_kwh_today: float = 0.0
    intervals_today: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HouseholdTariffState:
    active_bucket: str
    active_label: str
    active_rate: Optional[float]
    next_lower_cost_window: Optional[str]
    lowest_cost_window: Optional[str]
    import_rates: dict[str, float] = field(default_factory=dict)
    configured: bool = False
    source: str = "settings"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HouseholdEVState:
    present: bool = False
    connected: Optional[bool] = None
    charging_active: Optional[bool] = None
    charging_source: Optional[str] = None
    charging_power_w: Optional[float] = None
    charging_kwh_billing_period: float = 0.0
    evidence_note: str = "No live EV connection state is available."

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HouseholdSolarState:
    present: bool = False
    producing: Optional[bool] = None
    generated_kwh_today: float = 0.0
    exported_kwh_today: float = 0.0
    export_pct_today: float = 0.0
    current_production_w: Optional[float] = None
    source: str = "provider:solaredge"
    evidence_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HouseholdBatteryState:
    present: bool = False
    state: str = "not_detected"
    charge_pct: Optional[float] = None
    power_w: Optional[float] = None
    source: str = "capability_metadata"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HouseholdBillingState:
    period_start: Optional[str]
    period_end: Optional[str]
    day_of_period: int
    supply_days: int
    reading_days: int
    net_cost: float
    source: str = "billing_metadata"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HouseholdCollectorState:
    healthy: bool
    status: str
    latest_measurement_time: Optional[str]
    report_age_minutes: Optional[float]
    message: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HouseholdEvidenceState:
    evidence_context: str
    confidence: str
    quality: str
    freshness: str
    data_sources_used: list[str] = field(default_factory=list)
    trusted_provider_data: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HouseholdStateSummary:
    generated_at: str
    household_id: Optional[str]
    current_status: str
    current_tariff: str
    import_export_state: str
    evidence_confidence: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HouseholdState:
    generated_at: str
    household_id: Optional[str]
    summary: HouseholdStateSummary
    power: HouseholdPowerState
    energy: HouseholdEnergyState
    tariff: HouseholdTariffState
    ev: HouseholdEVState
    solar: HouseholdSolarState
    battery: HouseholdBatteryState
    billing: HouseholdBillingState
    collector: HouseholdCollectorState
    evidence: HouseholdEvidenceState

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "household_id": self.household_id,
            "summary": self.summary.as_dict(),
            "power": self.power.as_dict(),
            "energy": self.energy.as_dict(),
            "tariff": self.tariff.as_dict(),
            "ev": self.ev.as_dict(),
            "solar": self.solar.as_dict(),
            "battery": self.battery.as_dict(),
            "billing": self.billing.as_dict(),
            "collector": self.collector.as_dict(),
            "evidence": self.evidence.as_dict(),
        }


class HouseholdStateEngine:
    """Build canonical current facts from existing platform outputs."""

    def build(
        self,
        *,
        today_summary: Any,
        billing_summary: Any,
        settings: Any,
        health: dict[str, Any],
        capability_summary: Any = None,
        provider_management: Any = None,
        provider_runtime: Any = None,
        provider_observations: Optional[dict[str, Any]] = None,
        promotion_summary: Optional[dict[str, Any]] = None,
        household_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> HouseholdState:
        observed_at = now or datetime.now().astimezone()
        generated_at = observed_at.isoformat(timespec="seconds")
        tariff = self._tariff_state(settings, observed_at)
        live = _live_observations(provider_observations or {}, promotion_summary or {})
        power = self._power_state(today_summary, live)
        energy = self._energy_state(today_summary)
        solar = self._solar_state(today_summary, capability_summary, observed_at, live)
        ev = self._ev_state(billing_summary, capability_summary, live)
        battery = self._battery_state(capability_summary, live)
        billing = self._billing_state(billing_summary, settings, observed_at)
        collector = self._collector_state(health)
        evidence = self._evidence_state(
            today_summary=today_summary,
            billing_summary=billing_summary,
            health=health,
            tariff=tariff,
            provider_management=provider_management,
            provider_runtime=provider_runtime,
            provider_observations=provider_observations or {},
            promotion_summary=promotion_summary or {},
        )
        summary = HouseholdStateSummary(
            generated_at=generated_at,
            household_id=household_id,
            current_status="healthy" if collector.healthy and evidence.confidence in {"HIGH", "MEDIUM"} else "limited",
            current_tariff=tariff.active_label,
            import_export_state=power.import_export_state,
            evidence_confidence=evidence.confidence,
        )
        return HouseholdState(
            generated_at=generated_at,
            household_id=household_id,
            summary=summary,
            power=power,
            energy=energy,
            tariff=tariff,
            ev=ev,
            solar=solar,
            battery=battery,
            billing=billing,
            collector=collector,
            evidence=evidence,
        )

    def _tariff_state(self, settings: Any, observed_at: datetime) -> HouseholdTariffState:
        rates = import_rates(settings)
        bucket = current_tariff_bucket(observed_at)
        current_rate = rates.get(bucket)
        next_window = next_lower_cost_window(observed_at, bucket, rates) if rates else None
        lowest_window = lowest_cost_window(observed_at, bucket, rates) if rates else None
        return HouseholdTariffState(
            active_bucket=bucket,
            active_label=_tariff_label(bucket),
            active_rate=current_rate,
            next_lower_cost_window=next_window.display(observed_at) if next_window else None,
            lowest_cost_window=lowest_window.display(observed_at) if lowest_window else None,
            import_rates=rates,
            configured=bool(rates),
        )

    def _power_state(self, today_summary: Any, live: dict[str, dict[str, Any]]) -> HouseholdPowerState:
        imported = number(today_summary, "grid_import_kwh")
        exported = number(today_summary, "solar_exported_kwh")
        grid_power = live_number(live, "grid_live_power")
        home_power = live_number(live, "home_live_power")
        if grid_power is not None:
            state = "exporting" if grid_power < 0 else "importing" if grid_power > 0 else "balanced"
        elif exported > imported and exported > 0:
            state = "exporting"
        elif imported > 0:
            state = "importing"
        else:
            state = "unknown"
        return HouseholdPowerState(
            importing=state == "importing",
            exporting=state == "exporting",
            import_export_state=state,
            grid_import_kwh_today=imported,
            solar_exported_kwh_today=exported,
            current_grid_power_w=grid_power,
            current_home_power_w=home_power,
            source="live_provider_observation" if grid_power is not None or home_power is not None else "daily_summary",
        )

    def _energy_state(self, today_summary: Any) -> HouseholdEnergyState:
        return HouseholdEnergyState(
            solar_generated_kwh_today=number(today_summary, "solar_generated_kwh"),
            solar_used_kwh_today=number(today_summary, "solar_used_kwh"),
            solar_exported_kwh_today=number(today_summary, "solar_exported_kwh"),
            grid_import_kwh_today=number(today_summary, "grid_import_kwh"),
            intervals_today=int(number(today_summary, "intervals")),
        )

    def _solar_state(self, today_summary: Any, capability_summary: Any, observed_at: datetime, live: dict[str, dict[str, Any]]) -> HouseholdSolarState:
        generated = number(today_summary, "solar_generated_kwh")
        intervals = int(number(today_summary, "intervals"))
        solar_capability = capability(capability_summary, "solar")
        present = bool(getattr(solar_capability, "present", False) or generated > 0)
        daylight = 6 <= observed_at.astimezone().hour < 18
        producing = None
        solar_power = live_number(live, "solar_live_power")
        if solar_power is not None:
            producing = solar_power > 0
        elif intervals > 0:
            producing = bool(generated > 0 and daylight)
        return HouseholdSolarState(
            present=present,
            producing=producing,
            generated_kwh_today=generated,
            exported_kwh_today=number(today_summary, "solar_exported_kwh"),
            export_pct_today=number(today_summary, "solar_export_pct"),
            current_production_w=solar_power,
            source="live_provider_observation" if solar_power is not None else "provider:solaredge",
            evidence_note=getattr(solar_capability, "reason", "") if solar_capability else "",
        )

    def _ev_state(self, billing_summary: Any, capability_summary: Any, live: dict[str, dict[str, Any]]) -> HouseholdEVState:
        ev_capability = capability(capability_summary, "ev")
        source = string_value(billing_summary, "ev_charging_source")
        connected = live_value(live, "ev_connected_state")
        charging = live_value(live, "ev_charging_state")
        charging_power = live_number(live, "ev_charger_live_power")
        return HouseholdEVState(
            present=bool(getattr(ev_capability, "present", False) or number(billing_summary, "ev_charging_kwh") > 0),
            connected=connected if isinstance(connected, bool) else None,
            charging_active=charging if isinstance(charging, bool) else (charging_power > 0 if charging_power is not None else None),
            charging_source=source or None,
            charging_power_w=charging_power,
            charging_kwh_billing_period=number(billing_summary, "ev_charging_kwh"),
            evidence_note=getattr(ev_capability, "reason", "No live EV state is available.") if ev_capability else "No live EV state is available.",
        )

    def _battery_state(self, capability_summary: Any, live: dict[str, dict[str, Any]]) -> HouseholdBatteryState:
        battery = capability(capability_summary, "battery")
        present = bool(getattr(battery, "present", False))
        soc = live_number(live, "battery_soc")
        power = live_number(live, "battery_power")
        if soc is not None or power is not None:
            present = True
        state = "not_detected"
        if present:
            state = "discharging" if power and power < 0 else "charging" if power and power > 0 else "present_unknown_state"
        return HouseholdBatteryState(
            present=present,
            state=state,
            charge_pct=soc,
            power_w=power,
            source="live_provider_observation" if soc is not None or power is not None else getattr(battery, "source", "capability_metadata") if battery else "capability_metadata",
        )

    def _billing_state(self, billing_summary: Any, settings: Any, observed_at: datetime) -> HouseholdBillingState:
        start = getattr(settings, "current_billing_period_start", None)
        end = getattr(settings, "current_billing_period_end", None)
        if isinstance(settings, dict):
            start = settings.get("current_billing_period_start")
            end = settings.get("current_billing_period_end")
        start_date = parse_date(start)
        day_of_period = (observed_at.date() - start_date).days + 1 if start_date else int(number(billing_summary, "reading_days"))
        return HouseholdBillingState(
            period_start=start_date.isoformat() if start_date else None,
            period_end=parse_date(end).isoformat() if parse_date(end) else None,
            day_of_period=max(day_of_period, 0),
            supply_days=int(number(billing_summary, "supply_days")),
            reading_days=int(number(billing_summary, "reading_days")),
            net_cost=number(billing_summary, "net_cost"),
        )

    def _collector_state(self, health: dict[str, Any]) -> HouseholdCollectorState:
        status = str(health.get("status") or "unknown")
        return HouseholdCollectorState(
            healthy=status.lower() == "ok",
            status=status,
            latest_measurement_time=health.get("latest_measurement_time"),
            report_age_minutes=float(health["report_age_minutes"]) if health.get("report_age_minutes") is not None else None,
            message=health.get("collector_message") or health.get("message") or health.get("failure_type"),
        )

    def _evidence_state(
        self,
        *,
        today_summary: Any,
        billing_summary: Any,
        health: dict[str, Any],
        tariff: HouseholdTariffState,
        provider_management: Any,
        provider_runtime: Any,
        provider_observations: dict[str, Any],
        promotion_summary: dict[str, Any],
    ) -> HouseholdEvidenceState:
        status = str(health.get("status") or "").lower()
        intervals = int(number(today_summary, "intervals"))
        billing_intervals = int(number(billing_summary, "intervals"))
        age = health.get("report_age_minutes")
        if status != "ok" or (intervals <= 0 and billing_intervals <= 0):
            evidence_context = "INSUFFICIENT"
            confidence = "UNKNOWN"
            freshness = status or "No interval readings available"
        elif age is not None and float(age) > 180:
            evidence_context = "HISTORICAL_ONLY"
            confidence = "LOW"
            freshness = f"Report age {float(age):.0f} minutes"
        elif string_value(billing_summary, "ev_charging_source") == "estimated":
            evidence_context = "ESTIMATED"
            confidence = "LOW"
            freshness = "EV charging uses estimated fallback data"
        elif age is not None and float(age) > 15:
            evidence_context = "RECENT"
            confidence = "MEDIUM"
            freshness = f"Report age {float(age):.0f} minutes"
        else:
            evidence_context = "LIVE"
            confidence = "HIGH" if tariff.configured else "MEDIUM"
            freshness = f"Report age {float(age):.0f} minutes" if age is not None else "Fresh"
        sources = ["Household status", "Live data status"]
        if tariff.configured:
            sources.append("Configured tariff schedule")
        if provider_observations.get("count"):
            sources.append("Source readings")
        if promotion_summary.get("latest_promotions"):
            sources.append("Promotion Engine")
        trusted = []
        if provider_management:
            trusted.extend(provider.provider_id for provider in provider_management.providers() if provider.capability_summary.detected)
        if provider_runtime:
            trusted.extend(provider_runtime.as_dict().get("active_providers", []))
        quality = "HIGH" if confidence == "HIGH" else "MEDIUM" if confidence == "MEDIUM" else "LOW" if confidence == "LOW" else "UNKNOWN"
        return HouseholdEvidenceState(
            evidence_context=evidence_context,
            confidence=confidence,
            quality=quality,
            freshness=freshness,
            data_sources_used=sorted(set(sources)),
            trusted_provider_data=sorted(set(trusted)),
        )


def number(source: Any, name: str, default: float = 0.0) -> float:
    if source is None:
        return default
    value = source.get(name, default) if isinstance(source, dict) else getattr(source, name, default)
    return float(value or default)


def string_value(source: Any, name: str) -> str:
    if source is None:
        return ""
    value = source.get(name, "") if isinstance(source, dict) else getattr(source, name, "")
    return str(value or "")


def capability(summary: Any, capability_id: str) -> Any:
    if summary is None:
        return None
    if hasattr(summary, "get"):
        return summary.get(capability_id)
    return None


def _live_observations(provider_observations: dict[str, Any], promotion_summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    live: dict[str, dict[str, Any]] = {}
    for item in promotion_summary.get("latest_promotions", []) if isinstance(promotion_summary, dict) else []:
        if item.get("state") != "ACCEPTED":
            continue
        observation_type = str(item.get("observation_type") or "")
        if observation_type in LIVE_TYPES:
            live.setdefault(observation_type, item)
    for item in provider_observations.get("latest", []) if isinstance(provider_observations, dict) else []:
        observation_type = str(item.get("observation_type") or "")
        if observation_type in LIVE_TYPES:
            live.setdefault(observation_type, item)
    return live


def live_value(live: dict[str, dict[str, Any]], observation_type: str) -> Any:
    item = live.get(observation_type) or {}
    return item.get("value")


def live_number(live: dict[str, dict[str, Any]], observation_type: str) -> Optional[float]:
    value = live_value(live, observation_type)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _tariff_label(bucket: str) -> str:
    return {
        "overnight": "Overnight",
        "off_peak": "Off peak",
        "super_off_peak": "Super off peak",
        "peak": "Peak",
    }.get(bucket, bucket.replace("_", " ").title())


LIVE_TYPES = {
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
