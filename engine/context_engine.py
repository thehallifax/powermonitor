#!/usr/bin/env python3
"""Descriptive household context classification for PowerMonitor.

The Context Engine answers "what is happening now?" for advisors and
presentation layers. It does not recommend actions, optimise costs, or perform
financial calculations.
"""

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, Optional


class TimeContext(str, Enum):
    NIGHT = "NIGHT"
    EARLY_MORNING = "EARLY_MORNING"
    MORNING = "MORNING"
    MIDDAY = "MIDDAY"
    AFTERNOON = "AFTERNOON"
    EVENING = "EVENING"


class SolarContext(str, Enum):
    SOLAR_IMPOSSIBLE = "SOLAR_IMPOSSIBLE"
    SOLAR_AVAILABLE = "SOLAR_AVAILABLE"
    SOLAR_SURPLUS_POSSIBLE = "SOLAR_SURPLUS_POSSIBLE"
    LOW_SOLAR_DAY = "LOW_SOLAR_DAY"
    UNKNOWN = "UNKNOWN"


class TariffContext(str, Enum):
    HIGHEST_COST = "HIGHEST_COST"
    HIGH_COST = "HIGH_COST"
    NORMAL_COST = "NORMAL_COST"
    LOW_COST = "LOW_COST"
    LOWEST_COST = "LOWEST_COST"


class ChargingContext(str, Enum):
    CHARGING_ACTIVE = "CHARGING_ACTIVE"
    READY_TO_CHARGE = "READY_TO_CHARGE"
    WAITING_FOR_CHEAPER_WINDOW = "WAITING_FOR_CHEAPER_WINDOW"
    CHEAP_WINDOW_ACTIVE = "CHEAP_WINDOW_ACTIVE"
    UNKNOWN = "UNKNOWN"


class BillingContext(str, Enum):
    ON_TRACK = "ON_TRACK"
    HIGH_SPEND = "HIGH_SPEND"
    BILL_END_APPROACHING = "BILL_END_APPROACHING"
    NEW_BILLING_PERIOD = "NEW_BILLING_PERIOD"


class EvidenceContext(str, Enum):
    LIVE = "LIVE"
    RECENT = "RECENT"
    HISTORICAL_ONLY = "HISTORICAL_ONLY"
    ESTIMATED = "ESTIMATED"
    INSUFFICIENT = "INSUFFICIENT"


class ContextConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


TARIFF_LABELS = {
    "overnight": "Overnight",
    "off_peak": "Off peak",
    "super_off_peak": "Super off peak",
    "peak": "Peak",
}


@dataclass(frozen=True)
class TariffWindow:
    bucket: str
    label: str
    start: Optional[datetime]
    rate: Optional[float]

    def display(self, reference: datetime) -> str:
        if self.start is None:
            return self.label
        if self.start <= reference:
            return f"{self.label} now"
        return f"{self.label} from {self.start.strftime('%-I:%M %p')}"


@dataclass(frozen=True)
class HouseholdContext:
    observed_at: str
    time: TimeContext
    solar: SolarContext
    tariff: TariffContext
    charging: ChargingContext
    billing: BillingContext
    evidence: EvidenceContext
    confidence: ContextConfidence
    confidence_reason: str
    current_tariff_bucket: Optional[str] = None
    current_tariff_label: Optional[str] = None
    current_tariff_rate: Optional[float] = None
    next_lower_cost_window: Optional[str] = None
    lowest_cost_window: Optional[str] = None
    daylight: bool = False
    data_sources_used: tuple[str, ...] = ()
    freshness: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("time", "solar", "tariff", "charging", "billing", "evidence", "confidence"):
            data[key] = data[key].value
        data["data_sources_used"] = list(self.data_sources_used)
        return data


class ContextEngine:
    """Build a descriptive current household context from existing summaries."""

    def build(
        self,
        *,
        today_summary: Any,
        billing_summary: Any,
        settings: Any,
        health: dict[str, Any],
        now: Optional[datetime] = None,
        household_state: Any = None,
    ) -> HouseholdContext:
        observed_at = now or datetime.now().astimezone()
        if household_state is not None:
            return self._from_household_state(household_state, settings, observed_at)
        rates = import_rates(settings)
        bucket = current_tariff_bucket(observed_at)
        current_rate = rates.get(bucket)
        next_window = next_lower_cost_window(observed_at, bucket, rates)
        lowest_window = lowest_cost_window(observed_at, bucket, rates)
        evidence, freshness = self._evidence_context(today_summary, billing_summary, health)
        time_context = self._time_context(observed_at)
        solar_context = self._solar_context(today_summary, time_context, evidence)
        tariff_context = self._tariff_context(bucket, rates)
        billing_context = self._billing_context(billing_summary, settings, observed_at)
        charging_context = self._charging_context(evidence, tariff_context, next_window, observed_at)
        confidence = self._confidence(evidence, rates, today_summary, health)

        return HouseholdContext(
            observed_at=observed_at.astimezone().isoformat(timespec="seconds"),
            time=time_context,
            solar=solar_context,
            tariff=tariff_context,
            charging=charging_context,
            billing=billing_context,
            evidence=evidence,
            confidence=confidence,
            confidence_reason=self._confidence_reason(confidence, evidence, freshness, bool(rates)),
            current_tariff_bucket=bucket,
            current_tariff_label=TARIFF_LABELS.get(bucket),
            current_tariff_rate=current_rate,
            next_lower_cost_window=next_window.display(observed_at),
            lowest_cost_window=lowest_window.display(observed_at),
            daylight=time_context in {TimeContext.MORNING, TimeContext.MIDDAY, TimeContext.AFTERNOON},
            data_sources_used=self._data_sources(settings, health, evidence),
            freshness=freshness,
        )

    def _from_household_state(self, household_state: Any, settings: Any, observed_at: datetime) -> HouseholdContext:
        rates = getattr(household_state.tariff, "import_rates", None) or import_rates(settings)
        bucket = household_state.tariff.active_bucket
        next_window = next_lower_cost_window(observed_at, bucket, rates) if rates else None
        lowest_window = lowest_cost_window(observed_at, bucket, rates) if rates else None
        evidence = EvidenceContext(getattr(household_state.evidence, "evidence_context", "INSUFFICIENT"))
        freshness = household_state.evidence.freshness
        time_context = self._time_context(observed_at)
        solar_context = self._solar_context_from_state(household_state, time_context, evidence)
        tariff_context = self._tariff_context(bucket, rates)
        billing_context = self._billing_context_from_state(household_state, observed_at)
        charging_context = (
            ChargingContext.CHARGING_ACTIVE
            if getattr(household_state.ev, "charging_active", None) is True
            else self._charging_context(evidence, tariff_context, next_window or TariffWindow(bucket, TARIFF_LABELS.get(bucket, bucket), observed_at, household_state.tariff.active_rate), observed_at)
        )
        confidence = ContextConfidence(getattr(household_state.evidence, "confidence", "UNKNOWN"))
        return HouseholdContext(
            observed_at=observed_at.astimezone().isoformat(timespec="seconds"),
            time=time_context,
            solar=solar_context,
            tariff=tariff_context,
            charging=charging_context,
            billing=billing_context,
            evidence=evidence,
            confidence=confidence,
            confidence_reason=self._confidence_reason(confidence, evidence, freshness, bool(rates)),
            current_tariff_bucket=bucket,
            current_tariff_label=household_state.tariff.active_label,
            current_tariff_rate=household_state.tariff.active_rate,
            next_lower_cost_window=next_window.display(observed_at) if next_window else household_state.tariff.next_lower_cost_window,
            lowest_cost_window=lowest_window.display(observed_at) if lowest_window else household_state.tariff.lowest_cost_window,
            daylight=time_context in {TimeContext.MORNING, TimeContext.MIDDAY, TimeContext.AFTERNOON},
            data_sources_used=tuple(household_state.evidence.data_sources_used),
            freshness=freshness,
        )

    def _solar_context_from_state(self, household_state: Any, time_context: TimeContext, evidence: EvidenceContext) -> SolarContext:
        if evidence == EvidenceContext.INSUFFICIENT:
            return SolarContext.UNKNOWN
        if time_context == TimeContext.NIGHT:
            return SolarContext.SOLAR_IMPOSSIBLE
        solar_power = getattr(household_state.solar, "current_production_w", None)
        grid_power = getattr(household_state.power, "current_grid_power_w", None)
        if solar_power is not None and solar_power > 0 and grid_power is not None and grid_power < 0:
            return SolarContext.SOLAR_SURPLUS_POSSIBLE
        if solar_power is not None and solar_power > 0:
            return SolarContext.SOLAR_AVAILABLE
        if household_state.solar.producing is False:
            return SolarContext.LOW_SOLAR_DAY
        if household_state.solar.exported_kwh_today >= 0.5 and household_state.solar.export_pct_today >= 15:
            return SolarContext.SOLAR_SURPLUS_POSSIBLE
        if household_state.solar.producing or household_state.solar.generated_kwh_today > 0:
            return SolarContext.SOLAR_AVAILABLE
        return SolarContext.UNKNOWN

    def _billing_context_from_state(self, household_state: Any, observed_at: datetime) -> BillingContext:
        if household_state.billing.reading_days <= 2:
            return BillingContext.NEW_BILLING_PERIOD
        period_end = household_state.billing.period_end
        if period_end:
            end_date = date.fromisoformat(period_end)
            if 0 <= (end_date - observed_at.date()).days <= 3:
                return BillingContext.BILL_END_APPROACHING
        return BillingContext.ON_TRACK

    def _time_context(self, observed_at: datetime) -> TimeContext:
        current_time = observed_at.astimezone().time()
        if current_time >= time(23, 0) or current_time < time(6, 0):
            return TimeContext.NIGHT
        if current_time < time(9, 0):
            return TimeContext.EARLY_MORNING
        if current_time < time(12, 0):
            return TimeContext.MORNING
        if current_time < time(15, 0):
            return TimeContext.MIDDAY
        if current_time < time(18, 0):
            return TimeContext.AFTERNOON
        return TimeContext.EVENING

    def _solar_context(self, today_summary: Any, time_context: TimeContext, evidence: EvidenceContext) -> SolarContext:
        intervals = int(number(today_summary, "intervals"))
        if intervals <= 0 or evidence == EvidenceContext.INSUFFICIENT:
            return SolarContext.UNKNOWN
        if time_context == TimeContext.NIGHT:
            return SolarContext.SOLAR_IMPOSSIBLE
        generated = number(today_summary, "solar_generated_kwh")
        exported = number(today_summary, "solar_exported_kwh")
        export_pct = number(today_summary, "solar_export_pct")
        if generated <= 0:
            return SolarContext.LOW_SOLAR_DAY
        if exported >= 0.5 and export_pct >= 15:
            return SolarContext.SOLAR_SURPLUS_POSSIBLE
        return SolarContext.SOLAR_AVAILABLE

    def _tariff_context(self, bucket: str, rates: dict[str, float]) -> TariffContext:
        current_rate = rates.get(bucket)
        if current_rate is None or not rates:
            return TariffContext.NORMAL_COST
        ordered_rates = sorted(set(rates.values()))
        if len(ordered_rates) == 1:
            return TariffContext.NORMAL_COST
        rank = ordered_rates.index(current_rate)
        if rank == len(ordered_rates) - 1:
            return TariffContext.HIGHEST_COST
        if rank == 0:
            return TariffContext.LOWEST_COST
        if rank >= len(ordered_rates) - 2:
            return TariffContext.HIGH_COST
        if rank <= 1:
            return TariffContext.LOW_COST
        return TariffContext.NORMAL_COST

    def _charging_context(
        self,
        evidence: EvidenceContext,
        tariff_context: TariffContext,
        next_window: TariffWindow,
        observed_at: datetime,
    ) -> ChargingContext:
        if evidence == EvidenceContext.INSUFFICIENT:
            return ChargingContext.UNKNOWN
        if tariff_context == TariffContext.LOWEST_COST:
            return ChargingContext.CHEAP_WINDOW_ACTIVE
        if tariff_context == TariffContext.LOW_COST:
            return ChargingContext.READY_TO_CHARGE
        if next_window.start and next_window.start > observed_at:
            return ChargingContext.WAITING_FOR_CHEAPER_WINDOW
        return ChargingContext.UNKNOWN

    def _billing_context(self, summary: Any, settings: Any, observed_at: datetime) -> BillingContext:
        reading_days = int(number(summary, "reading_days"))
        if reading_days <= 2:
            return BillingContext.NEW_BILLING_PERIOD
        period_end = getattr(settings, "current_billing_period_end", None)
        if isinstance(settings, dict):
            period_end = settings.get("current_billing_period_end")
        if isinstance(period_end, str):
            period_end = date.fromisoformat(period_end)
        if period_end and 0 <= (period_end - observed_at.date()).days <= 3:
            return BillingContext.BILL_END_APPROACHING
        thresholds = getattr(settings, "recommendation_thresholds", {}) or {}
        if isinstance(settings, dict):
            thresholds = settings.get("recommendation_thresholds", {}) or {}
        high_cost_threshold = thresholds.get("high_cost_threshold") or thresholds.get("high_bill_threshold")
        if high_cost_threshold is not None and number(summary, "net_cost") >= float(high_cost_threshold):
            return BillingContext.HIGH_SPEND
        return BillingContext.ON_TRACK

    def _evidence_context(self, today_summary: Any, billing_summary: Any, health: dict[str, Any]) -> tuple[EvidenceContext, str]:
        status = str(health.get("status") or "").lower()
        intervals = int(number(today_summary, "intervals"))
        billing_intervals = int(number(billing_summary, "intervals"))
        if status not in {"ok"} or (intervals <= 0 and billing_intervals <= 0):
            return EvidenceContext.INSUFFICIENT, status or "No interval readings available"
        ev_source = str(getattr(billing_summary, "ev_charging_source", "") or "")
        if isinstance(billing_summary, dict):
            ev_source = str(billing_summary.get("ev_charging_source") or "")
        if ev_source == "estimated":
            return EvidenceContext.ESTIMATED, "EV charging uses estimated fallback data"
        age = health.get("report_age_minutes")
        if age is None:
            return EvidenceContext.RECENT, "Report age unavailable"
        age_value = float(age)
        if age_value <= 15:
            return EvidenceContext.LIVE, f"Report age {age_value:.0f} minutes"
        if age_value <= 180:
            return EvidenceContext.RECENT, f"Report age {age_value:.0f} minutes"
        return EvidenceContext.HISTORICAL_ONLY, f"Report age {age_value:.0f} minutes"

    def _confidence(
        self,
        evidence: EvidenceContext,
        rates: dict[str, float],
        today_summary: Any,
        health: dict[str, Any],
    ) -> ContextConfidence:
        if evidence == EvidenceContext.INSUFFICIENT:
            return ContextConfidence.UNKNOWN
        if not rates:
            return ContextConfidence.LOW
        if evidence in {EvidenceContext.HISTORICAL_ONLY, EvidenceContext.ESTIMATED}:
            return ContextConfidence.LOW
        if int(number(today_summary, "intervals")) <= 0 or str(health.get("status") or "").lower() != "ok":
            return ContextConfidence.LOW
        if evidence == EvidenceContext.RECENT:
            return ContextConfidence.MEDIUM
        return ContextConfidence.HIGH

    def _confidence_reason(
        self,
        confidence: ContextConfidence,
        evidence: EvidenceContext,
        freshness: str,
        has_tariff: bool,
    ) -> str:
        if confidence == ContextConfidence.UNKNOWN:
            return "Context is unknown because required household evidence is unavailable."
        if not has_tariff:
            return "Context confidence is reduced because tariff configuration is missing."
        if evidence == EvidenceContext.ESTIMATED:
            return "Context confidence is reduced because some household data is estimated."
        if evidence == EvidenceContext.HISTORICAL_ONLY:
            return f"Context confidence is reduced because data is historical only: {freshness}."
        if confidence == ContextConfidence.MEDIUM:
            return f"Context is usable, but freshness is limited: {freshness}."
        return "Context uses configured tariff data and fresh household interval evidence."

    def _data_sources(self, settings: Any, health: dict[str, Any], evidence: EvidenceContext) -> tuple[str, ...]:
        sources = ["Collector health"]
        if import_rates(settings):
            sources.append("Configured tariff schedule")
        if evidence != EvidenceContext.INSUFFICIENT:
            sources.append("Daily summary")
        status = str(health.get("status") or "").lower()
        if status == "ok":
            sources.append("Smart meter interval data")
        return tuple(sources)


def import_rates(settings: Any) -> dict[str, float]:
    rates = settings.get("import_rates", {}) if isinstance(settings, dict) else getattr(settings, "import_rates", {})
    return {bucket: float(value) for bucket, value in (rates or {}).items()}


def number(source: Any, name: str) -> float:
    if isinstance(source, dict):
        value = source.get(name, 0.0)
    else:
        value = getattr(source, name, 0.0)
    return float(value or 0.0)


def current_tariff_bucket(now: datetime) -> str:
    current_time = now.astimezone().time()
    if current_time >= time(23, 0) or current_time < time(6, 0):
        return "overnight"
    if time(6, 0) <= current_time < time(9, 0):
        return "off_peak"
    if time(9, 0) <= current_time < time(15, 0):
        return "super_off_peak"
    if time(15, 0) <= current_time < time(21, 0):
        return "peak"
    return "off_peak"


def next_lower_cost_window(now: datetime, current_bucket: str, rates: dict[str, float]) -> TariffWindow:
    current_rate = rates.get(current_bucket)
    if current_rate is None:
        return TariffWindow(current_bucket, TARIFF_LABELS[current_bucket], now, None)
    if current_rate <= min(rates.values(), default=current_rate):
        return TariffWindow(current_bucket, TARIFF_LABELS[current_bucket], now, current_rate)
    for start, bucket in upcoming_tariff_windows(now):
        rate = rates.get(bucket)
        if rate is not None and rate < current_rate:
            return TariffWindow(bucket, TARIFF_LABELS[bucket], start, rate)
    return TariffWindow(current_bucket, TARIFF_LABELS[current_bucket], now, current_rate)


def lowest_cost_window(now: datetime, current_bucket: str, rates: dict[str, float]) -> TariffWindow:
    current_rate = rates.get(current_bucket)
    if not rates:
        return TariffWindow(current_bucket, TARIFF_LABELS[current_bucket], now, current_rate)
    lowest_rate = min(rates.values())
    if current_rate == lowest_rate:
        return TariffWindow(current_bucket, TARIFF_LABELS[current_bucket], now, current_rate)
    for start, bucket in upcoming_tariff_windows(now):
        rate = rates.get(bucket)
        if rate == lowest_rate:
            return TariffWindow(bucket, TARIFF_LABELS[bucket], start, rate)
    lowest_bucket = min(rates, key=lambda item: rates[item])
    return TariffWindow(lowest_bucket, TARIFF_LABELS[lowest_bucket], None, rates[lowest_bucket])


def upcoming_tariff_windows(now: datetime) -> list[tuple[datetime, str]]:
    local_now = now.astimezone()
    day = local_now.date()
    schedule = [
        (time(6, 0), "off_peak"),
        (time(9, 0), "super_off_peak"),
        (time(15, 0), "peak"),
        (time(21, 0), "off_peak"),
        (time(23, 0), "overnight"),
    ]
    windows: list[tuple[datetime, str]] = []
    for day_offset in range(3):
        target_day = day + timedelta(days=day_offset)
        for window_time, bucket in schedule:
            start = datetime.combine(target_day, window_time, tzinfo=local_now.tzinfo)
            if start > local_now:
                windows.append((start, bucket))
    windows.sort(key=lambda item: item[0])
    return windows


def wait_minutes(now: datetime, start: Optional[datetime]) -> int:
    if start is None or start <= now:
        return 0
    return int(round((start - now).total_seconds() / 60))
