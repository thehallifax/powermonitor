"""Provider-agnostic historical trend analysis for household energy data."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional


IMPROVING = "Improving"
WORSENING = "Worsening"
UNCHANGED = "Unchanged"


@dataclass(frozen=True)
class HistoricalTrendMetric:
    metric_id: str
    label: str
    domain: str
    value: Optional[float]
    unit: str
    comparison_value: Optional[float]
    difference_from_yesterday: Optional[float]
    percentage_difference: Optional[float]
    trend_direction: str
    date: Optional[str] = None
    sample_days: int = 0
    confidence: str = "Low"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HistoricalTrendGroup:
    domain: str
    metrics: dict[str, HistoricalTrendMetric] = field(default_factory=dict)
    available: bool = True
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "available": self.available,
            "reason": self.reason,
            "metrics": {key: metric.as_dict() for key, metric in self.metrics.items()},
        }


@dataclass(frozen=True)
class HistoricalTrends:
    as_of: str
    yesterday: str
    history_days: int
    cost: HistoricalTrendGroup
    grid: HistoricalTrendGroup
    solar: HistoricalTrendGroup
    exports: HistoricalTrendGroup
    self_consumption: HistoricalTrendGroup

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "yesterday": self.yesterday,
            "history_days": self.history_days,
            "cost": self.cost.as_dict(),
            "grid": self.grid.as_dict(),
            "solar": self.solar.as_dict(),
            "exports": self.exports.as_dict(),
            "self_consumption": self.self_consumption.as_dict(),
        }


class HistoricalTrendsEngine:
    """Build reusable historical analytics from normalized daily summaries."""

    name = "historical_trends_engine"

    def build(self, daily_history: Iterable[Any], *, as_of: Optional[date] = None) -> HistoricalTrends:
        rows = _normalise_history(daily_history)
        if as_of is None:
            latest_day = max((row["day"] for row in rows), default=date.today() - timedelta(days=1))
            as_of = latest_day + timedelta(days=1)
        yesterday = as_of - timedelta(days=1)
        rows = [row for row in rows if row["day"] < as_of]
        by_day = {row["day"]: row for row in rows}

        return HistoricalTrends(
            as_of=as_of.isoformat(),
            yesterday=yesterday.isoformat(),
            history_days=len(rows),
            cost=self._cost_group(rows, by_day, yesterday),
            grid=self._grid_group(rows, by_day, yesterday),
            solar=self._solar_group(rows, by_day, yesterday),
            exports=self._exports_group(rows, by_day, yesterday),
            self_consumption=self._self_consumption_group(rows, by_day, yesterday),
        )

    def _cost_group(
        self,
        rows: list[dict[str, Any]],
        by_day: dict[date, dict[str, Any]],
        yesterday: date,
    ) -> HistoricalTrendGroup:
        return HistoricalTrendGroup(
            domain="cost",
            metrics={
                "yesterday_cost": self._daily_metric(
                    "yesterday_cost",
                    "Yesterday's cost",
                    "cost",
                    "net_cost",
                    "$",
                    rows,
                    by_day,
                    yesterday,
                    higher_is_better=False,
                ),
                "rolling_7_day_average_cost": self._rolling_metric(
                    "rolling_7_day_average_cost",
                    "Rolling 7-day average cost",
                    "cost",
                    "net_cost",
                    "$",
                    rows,
                    by_day,
                    yesterday,
                    7,
                    higher_is_better=False,
                ),
                "rolling_30_day_average_cost": self._rolling_metric(
                    "rolling_30_day_average_cost",
                    "Rolling 30-day average cost",
                    "cost",
                    "net_cost",
                    "$",
                    rows,
                    by_day,
                    yesterday,
                    30,
                    higher_is_better=False,
                ),
                "highest_daily_cost": self._extreme_metric(
                    "highest_daily_cost",
                    "Highest daily cost",
                    "cost",
                    "net_cost",
                    "$",
                    rows,
                    by_day,
                    yesterday,
                    max,
                    higher_is_better=False,
                ),
                "lowest_daily_cost": self._extreme_metric(
                    "lowest_daily_cost",
                    "Lowest daily cost",
                    "cost",
                    "net_cost",
                    "$",
                    rows,
                    by_day,
                    yesterday,
                    min,
                    higher_is_better=False,
                ),
            },
        )

    def _grid_group(
        self,
        rows: list[dict[str, Any]],
        by_day: dict[date, dict[str, Any]],
        yesterday: date,
    ) -> HistoricalTrendGroup:
        return HistoricalTrendGroup(
            domain="grid",
            metrics={
                "yesterday_grid_import": self._daily_metric(
                    "yesterday_grid_import",
                    "Yesterday's grid import",
                    "grid",
                    "grid_import_kwh",
                    "kWh",
                    rows,
                    by_day,
                    yesterday,
                    higher_is_better=False,
                ),
                "rolling_7_day_average_import": self._rolling_metric(
                    "rolling_7_day_average_import",
                    "Rolling 7-day average import",
                    "grid",
                    "grid_import_kwh",
                    "kWh",
                    rows,
                    by_day,
                    yesterday,
                    7,
                    higher_is_better=False,
                ),
                "rolling_30_day_average_import": self._rolling_metric(
                    "rolling_30_day_average_import",
                    "Rolling 30-day average import",
                    "grid",
                    "grid_import_kwh",
                    "kWh",
                    rows,
                    by_day,
                    yesterday,
                    30,
                    higher_is_better=False,
                ),
                "highest_import_day": self._extreme_metric(
                    "highest_import_day",
                    "Highest import day",
                    "grid",
                    "grid_import_kwh",
                    "kWh",
                    rows,
                    by_day,
                    yesterday,
                    max,
                    higher_is_better=False,
                ),
                "lowest_import_day": self._extreme_metric(
                    "lowest_import_day",
                    "Lowest import day",
                    "grid",
                    "grid_import_kwh",
                    "kWh",
                    rows,
                    by_day,
                    yesterday,
                    min,
                    higher_is_better=False,
                ),
            },
        )

    def _solar_group(
        self,
        rows: list[dict[str, Any]],
        by_day: dict[date, dict[str, Any]],
        yesterday: date,
    ) -> HistoricalTrendGroup:
        solar_rows = [row for row in rows if _number(row, "solar_generated_kwh") > 0]
        if not solar_rows:
            return HistoricalTrendGroup(domain="solar", available=False, reason="No solar generation history available.")
        return HistoricalTrendGroup(
            domain="solar",
            metrics={
                "yesterday_generation": self._daily_metric(
                    "yesterday_generation",
                    "Yesterday's generation",
                    "solar",
                    "solar_generated_kwh",
                    "kWh",
                    solar_rows,
                    by_day,
                    yesterday,
                    higher_is_better=True,
                ),
                "rolling_7_day_average_generation": self._rolling_metric(
                    "rolling_7_day_average_generation",
                    "Rolling 7-day average generation",
                    "solar",
                    "solar_generated_kwh",
                    "kWh",
                    solar_rows,
                    by_day,
                    yesterday,
                    7,
                    higher_is_better=True,
                ),
                "rolling_30_day_average_generation": self._rolling_metric(
                    "rolling_30_day_average_generation",
                    "Rolling 30-day average generation",
                    "solar",
                    "solar_generated_kwh",
                    "kWh",
                    solar_rows,
                    by_day,
                    yesterday,
                    30,
                    higher_is_better=True,
                ),
                "highest_solar_day": self._extreme_metric(
                    "highest_solar_day",
                    "Highest solar day",
                    "solar",
                    "solar_generated_kwh",
                    "kWh",
                    solar_rows,
                    by_day,
                    yesterday,
                    max,
                    higher_is_better=True,
                ),
                "lowest_solar_day": self._extreme_metric(
                    "lowest_solar_day",
                    "Lowest solar day",
                    "solar",
                    "solar_generated_kwh",
                    "kWh",
                    solar_rows,
                    by_day,
                    yesterday,
                    min,
                    higher_is_better=True,
                ),
            },
        )

    def _exports_group(
        self,
        rows: list[dict[str, Any]],
        by_day: dict[date, dict[str, Any]],
        yesterday: date,
    ) -> HistoricalTrendGroup:
        export_rows = [row for row in rows if _number(row, "solar_exported_kwh") > 0]
        if not export_rows:
            return HistoricalTrendGroup(domain="exports", available=False, reason="No export history available.")
        return HistoricalTrendGroup(
            domain="exports",
            metrics={
                "yesterday_exports": self._daily_metric(
                    "yesterday_exports",
                    "Yesterday's exports",
                    "exports",
                    "solar_exported_kwh",
                    "kWh",
                    export_rows,
                    by_day,
                    yesterday,
                    higher_is_better=True,
                ),
                "rolling_7_day_average_exports": self._rolling_metric(
                    "rolling_7_day_average_exports",
                    "Rolling 7-day average exports",
                    "exports",
                    "solar_exported_kwh",
                    "kWh",
                    export_rows,
                    by_day,
                    yesterday,
                    7,
                    higher_is_better=True,
                ),
            },
        )

    def _self_consumption_group(
        self,
        rows: list[dict[str, Any]],
        by_day: dict[date, dict[str, Any]],
        yesterday: date,
    ) -> HistoricalTrendGroup:
        self_rows = [dict(row, self_consumption_pct=_self_consumption_pct(row)) for row in rows]
        self_rows = [row for row in self_rows if row["self_consumption_pct"] is not None]
        if not self_rows:
            return HistoricalTrendGroup(
                domain="self_consumption",
                available=False,
                reason="No self-consumption history available.",
            )
        self_by_day = {row["day"]: row for row in self_rows}
        return HistoricalTrendGroup(
            domain="self_consumption",
            metrics={
                "rolling_average_self_consumption": self._rolling_metric(
                    "rolling_average_self_consumption",
                    "Rolling average self-consumption",
                    "self_consumption",
                    "self_consumption_pct",
                    "%",
                    self_rows,
                    self_by_day,
                    yesterday,
                    30,
                    higher_is_better=True,
                ),
                "best_self_consumption_day": self._extreme_metric(
                    "best_self_consumption_day",
                    "Best self-consumption day",
                    "self_consumption",
                    "self_consumption_pct",
                    "%",
                    self_rows,
                    self_by_day,
                    yesterday,
                    max,
                    higher_is_better=True,
                ),
            },
        )

    def _daily_metric(
        self,
        metric_id: str,
        label: str,
        domain: str,
        field_name: str,
        unit: str,
        rows: list[dict[str, Any]],
        by_day: dict[date, dict[str, Any]],
        yesterday: date,
        *,
        higher_is_better: bool,
    ) -> HistoricalTrendMetric:
        value = _field_value(by_day.get(yesterday), field_name)
        previous = _previous_value(rows, yesterday, field_name)
        return _metric(
            metric_id,
            label,
            domain,
            value,
            unit,
            previous,
            higher_is_better=higher_is_better,
            sample_days=1 if value is not None else 0,
            metric_date=yesterday if value is not None else None,
        )

    def _rolling_metric(
        self,
        metric_id: str,
        label: str,
        domain: str,
        field_name: str,
        unit: str,
        rows: list[dict[str, Any]],
        by_day: dict[date, dict[str, Any]],
        yesterday: date,
        days: int,
        *,
        higher_is_better: bool,
    ) -> HistoricalTrendMetric:
        start = yesterday - timedelta(days=days - 1)
        values = [
            _field_value(row, field_name)
            for row in rows
            if start <= row["day"] <= yesterday
        ]
        values = [value for value in values if value is not None]
        value = _average(values)
        comparison = _field_value(by_day.get(yesterday), field_name)
        return _metric(
            metric_id,
            label,
            domain,
            value,
            unit,
            comparison,
            higher_is_better=higher_is_better,
            sample_days=len(values),
        )

    def _extreme_metric(
        self,
        metric_id: str,
        label: str,
        domain: str,
        field_name: str,
        unit: str,
        rows: list[dict[str, Any]],
        by_day: dict[date, dict[str, Any]],
        yesterday: date,
        selector: Any,
        *,
        higher_is_better: bool,
    ) -> HistoricalTrendMetric:
        values = [(row, _field_value(row, field_name)) for row in rows if row["day"] <= yesterday]
        values = [(row, value) for row, value in values if value is not None]
        selected_row = None
        value = None
        if values:
            selected_row, value = selector(values, key=lambda item: item[1])
        comparison = _field_value(by_day.get(yesterday), field_name)
        return _metric(
            metric_id,
            label,
            domain,
            value,
            unit,
            comparison,
            higher_is_better=higher_is_better,
            sample_days=len(values),
            metric_date=selected_row["day"] if selected_row else None,
        )


def _metric(
    metric_id: str,
    label: str,
    domain: str,
    value: Optional[float],
    unit: str,
    comparison: Optional[float],
    *,
    higher_is_better: bool,
    sample_days: int,
    metric_date: Optional[date] = None,
) -> HistoricalTrendMetric:
    difference = None
    pct = None
    if value is not None and comparison is not None:
        difference = value - comparison
        pct = difference / comparison * 100 if comparison else 0.0
    return HistoricalTrendMetric(
        metric_id=metric_id,
        label=label,
        domain=domain,
        value=value,
        unit=unit,
        comparison_value=comparison,
        difference_from_yesterday=difference,
        percentage_difference=pct,
        trend_direction=_direction(difference, higher_is_better),
        date=metric_date.isoformat() if metric_date else None,
        sample_days=sample_days,
        confidence=_confidence(sample_days),
    )


def _normalise_history(history: Iterable[Any]) -> list[dict[str, Any]]:
    rows = []
    for item in history or []:
        day = _as_date(_value(item, "day"))
        if day is None:
            continue
        rows.append(
            {
                "day": day,
                "net_cost": _optional_number(item, "net_cost"),
                "grid_import_kwh": _optional_number(item, "grid_import_kwh"),
                "solar_generated_kwh": _optional_number(item, "solar_generated_kwh"),
                "solar_exported_kwh": _optional_number(item, "solar_exported_kwh"),
            }
        )
    deduped = {row["day"]: row for row in rows}
    return [deduped[day] for day in sorted(deduped)]


def _field_value(row: Optional[dict[str, Any]], field_name: str) -> Optional[float]:
    if not row:
        return None
    value = row.get(field_name)
    return float(value) if value is not None else None


def _previous_value(rows: list[dict[str, Any]], before_day: date, field_name: str) -> Optional[float]:
    previous = [
        row for row in rows
        if row["day"] < before_day and _field_value(row, field_name) is not None
    ]
    if not previous:
        return None
    return _field_value(previous[-1], field_name)


def _average(values: Iterable[float]) -> Optional[float]:
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def _direction(difference: Optional[float], higher_is_better: bool) -> str:
    if difference is None or abs(difference) < 0.000001:
        return UNCHANGED
    improved = difference > 0 if higher_is_better else difference < 0
    return IMPROVING if improved else WORSENING


def _confidence(sample_days: int) -> str:
    if sample_days >= 21:
        return "High"
    if sample_days >= 7:
        return "Medium"
    return "Low"


def _self_consumption_pct(row: dict[str, Any]) -> Optional[float]:
    generated = _number(row, "solar_generated_kwh")
    if generated <= 0:
        return None
    exported = max(_number(row, "solar_exported_kwh"), 0.0)
    return max(0.0, min(100.0, (generated - exported) / generated * 100))


def _number(item: Any, name: str) -> float:
    value = _field_value(item, name) if isinstance(item, dict) else _optional_number(item, name)
    return float(value or 0.0)


def _optional_number(item: Any, name: str) -> Optional[float]:
    value = _value(item, name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None
