#!/usr/bin/env python3
"""Bill reconciliation for imported retailer bills.

The reconciliation engine compares already-normalised imported bill data with
PowerMonitor's period estimate. It does not query SQLite or render UI.
"""

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional

from .financial_engine import FinancialEngine


@dataclass(frozen=True)
class BillVarianceComponent:
    category: str
    label: str
    actual_amount: float
    estimated_amount: float
    variance: float
    explanation: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BillReconciliation:
    bill_id: str
    invoice_number: str
    retailer: str
    period_start: Optional[str]
    period_end: Optional[str]
    actual_bill_total: Optional[float]
    estimated_bill_total: Optional[float]
    dollar_variance: Optional[float]
    percentage_variance: Optional[float]
    variance_category: str
    confidence: str
    explanation: str
    components: list[BillVarianceComponent] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)
    tariff_mismatches: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["components"] = [component.as_dict() for component in self.components]
        return result


class BillReconciliationEngine:
    """Compare imported Synergy bills against PowerMonitor estimates."""

    def __init__(self, financial_engine: Optional[FinancialEngine] = None) -> None:
        self.financial_engine = financial_engine or FinancialEngine()

    def reconcile(
        self,
        bill: dict[str, Any],
        estimate_summary: Any,
        settings: Any,
    ) -> BillReconciliation:
        periods = bill.get("periods") or []
        period_start, period_end = self._period_bounds(periods)
        actual_total = self._optional_float(bill.get("bill_total"))
        estimated_total = self._optional_float(self._value(estimate_summary, "net_cost"))

        dollar_variance = (
            self.financial_engine.calculate_bill_variance(actual_total, estimated_total)
            if actual_total is not None and estimated_total is not None
            else None
        )
        percentage_variance = (
            self.financial_engine.calculate_percentage_variance(dollar_variance, actual_total)
            if dollar_variance is not None and actual_total not in (None, 0)
            else None
        )
        components = self._components(bill, estimate_summary, actual_total, estimated_total)
        coverage = self._coverage(period_start, period_end, estimate_summary)
        tariff_mismatches = self._tariff_mismatches(bill, settings)
        data_gaps = self._data_gaps(bill, estimate_summary, coverage, tariff_mismatches)
        confidence = self._confidence(bill, coverage, data_gaps, tariff_mismatches, periods)
        variance_category = self._variance_category(dollar_variance, percentage_variance, confidence)
        explanation = self._explanation(dollar_variance, components, data_gaps, tariff_mismatches)

        return BillReconciliation(
            bill_id=str(bill.get("bill_id") or ""),
            invoice_number=str(bill.get("invoice_number") or ""),
            retailer=str(bill.get("retailer") or "Synergy"),
            period_start=period_start.isoformat() if period_start else None,
            period_end=period_end.isoformat() if period_end else None,
            actual_bill_total=actual_total,
            estimated_bill_total=estimated_total,
            dollar_variance=dollar_variance,
            percentage_variance=percentage_variance,
            variance_category=variance_category,
            confidence=confidence,
            explanation=explanation,
            components=components,
            data_gaps=data_gaps,
            tariff_mismatches=tariff_mismatches,
            coverage=coverage,
        )

    def _components(
        self,
        bill: dict[str, Any],
        estimate_summary: Any,
        actual_total: Optional[float],
        estimated_total: Optional[float],
    ) -> list[BillVarianceComponent]:
        totals = bill.get("totals") or {}
        actual_import = self._sum_items(bill, "import_usage")
        actual_export = self._sum_items(bill, "export_credit")
        actual_supply = self._sum_items(bill, "supply_charge")
        actual_gst = self._optional_float(bill.get("gst"))
        if actual_gst is None:
            actual_gst = self._optional_float(totals.get("gst")) or 0.0
        opening = self._optional_float(bill.get("opening_balance")) or 0.0

        estimated_import = self._number(estimate_summary, "import_cost")
        estimated_export = -self._number(estimate_summary, "export_credit")
        estimated_supply = self._number(estimate_summary, "supply_cost")
        estimated_gst = 0.0
        estimated_opening = 0.0

        components = [
            self._component("import_usage", "Usage charges", actual_import, estimated_import, "Grid energy charges on the retailer bill compared with PowerMonitor's tariff estimate."),
            self._component("export_credit", "Export credits", actual_export, estimated_export, "Solar buyback credits on the retailer bill compared with PowerMonitor's export estimate."),
            self._component("supply_charge", "Supply charge", actual_supply, estimated_supply, "Daily supply charge on the retailer bill compared with the configured daily supply charge."),
            self._component("gst", "GST", actual_gst, estimated_gst, "GST shown separately on the retailer bill. PowerMonitor tariff rates may already include GST."),
            self._component("opening_balance", "Opening balance / credits", opening, estimated_opening, "Opening balances, credits, or adjustments carried into the retailer bill."),
        ]
        known_variance = sum(component.variance for component in components)
        total_variance = (
            self.financial_engine.calculate_bill_variance(actual_total, estimated_total)
            if actual_total is not None and estimated_total is not None
            else 0.0
        )
        unexplained = total_variance - known_variance
        components.append(
            self._component(
                "unexplained",
                "Other difference",
                unexplained,
                0.0,
                "The remaining difference after matching usage, export credits, supply charges, GST, and adjustments.",
            )
        )
        return components

    def _component(self, category: str, label: str, actual: float, estimated: float, explanation: str) -> BillVarianceComponent:
        return BillVarianceComponent(
            category=category,
            label=label,
            actual_amount=round(float(actual), 2),
            estimated_amount=round(float(estimated), 2),
            variance=round(self.financial_engine.calculate_bill_variance(actual, estimated), 2),
            explanation=explanation,
        )

    def _coverage(self, period_start: Optional[date], period_end: Optional[date], summary: Any) -> dict[str, Any]:
        supply_days = self._number(summary, "supply_days")
        reading_days = self._number(summary, "reading_days")
        expected_days = supply_days
        if period_start and period_end:
            expected_days = max((period_end - period_start).days + 1, 0)
        coverage_ratio = reading_days / expected_days if expected_days else 0.0
        return {
            "expected_days": int(expected_days),
            "reading_days": int(reading_days),
            "missing_days": max(int(expected_days) - int(reading_days), 0),
            "coverage_ratio": round(coverage_ratio, 4),
            "estimate_start": self._iso(self._value(summary, "start")),
            "estimate_end": self._iso(self._value(summary, "end")),
        }

    def _data_gaps(
        self,
        bill: dict[str, Any],
        summary: Any,
        coverage: dict[str, Any],
        tariff_mismatches: list[str],
    ) -> list[str]:
        gaps = []
        if bill.get("bill_total") is None:
            gaps.append("The imported bill total was not found.")
        if str(bill.get("confidence") or "").lower() == "low":
            gaps.append("The bill parser had low confidence in the extracted values.")
        if coverage.get("missing_days", 0) > 0:
            gaps.append(f"PowerMonitor has readings for {coverage.get('reading_days', 0)} of {coverage.get('expected_days', 0)} bill days.")
        if str(self._value(summary, "ev_charging_source") or "") == "estimated":
            gaps.append("EV charging for this period uses an estimate rather than completed charger sessions.")
        if tariff_mismatches:
            gaps.append("Some tariff rates on the bill differ from the configured PowerMonitor rates.")
        return gaps

    def _confidence(
        self,
        bill: dict[str, Any],
        coverage: dict[str, Any],
        data_gaps: list[str],
        tariff_mismatches: list[str],
        periods: list[dict[str, Any]],
    ) -> str:
        score = 3
        if bill.get("bill_total") is None:
            score -= 2
        parser_confidence = str(bill.get("confidence") or "medium").lower()
        if parser_confidence == "low":
            score -= 2
        elif parser_confidence == "medium":
            score -= 1
        if float(coverage.get("coverage_ratio") or 0.0) < 0.8:
            score -= 1
        if tariff_mismatches:
            score -= 1
        if len(periods) > 1:
            score -= 1
        if any("EV charging" in gap for gap in data_gaps):
            score -= 1
        if score >= 3:
            return "high"
        if score >= 1:
            return "medium"
        return "low"

    def _variance_category(
        self,
        dollar_variance: Optional[float],
        percentage_variance: Optional[float],
        confidence: str,
    ) -> str:
        if dollar_variance is None or percentage_variance is None or confidence == "low":
            return "unreliable"
        abs_dollars = abs(dollar_variance)
        abs_pct = abs(percentage_variance)
        if abs_dollars <= 2.0 or abs_pct <= 1.0:
            return "excellent"
        if abs_dollars <= 10.0 or abs_pct <= 3.0:
            return "close"
        if abs_dollars <= 25.0 or abs_pct <= 8.0:
            return "needs review"
        return "unreliable"

    def _explanation(
        self,
        dollar_variance: Optional[float],
        components: list[BillVarianceComponent],
        data_gaps: list[str],
        tariff_mismatches: list[str],
    ) -> str:
        if dollar_variance is None:
            return "PowerMonitor cannot compare this bill yet because either the retailer total or the estimate is missing."
        if abs(dollar_variance) < 0.01:
            return "PowerMonitor matched this Synergy bill almost exactly."
        direction = "higher" if dollar_variance > 0 else "lower"
        leading = max(components, key=lambda component: abs(component.variance)) if components else None
        reason = f" The largest visible difference is {leading.label.lower()}." if leading and abs(leading.variance) >= 0.01 else ""
        if tariff_mismatches:
            reason += " Some bill tariff rates differ from the configured tariff."
        elif data_gaps:
            reason += f" {data_gaps[0]}"
        return f"The real Synergy bill was ${abs(dollar_variance):.2f} {direction} than PowerMonitor's estimate.{reason}".strip()

    def _tariff_mismatches(self, bill: dict[str, Any], settings: Any) -> list[str]:
        rates = self._value(settings, "import_rates", {}) or {}
        export_rate = self._optional_float(self._value(settings, "export_credit_rate"))
        mismatches = []
        for item in bill.get("line_items") or []:
            bucket = item.get("bucket")
            rate = self._optional_float(item.get("unit_rate"))
            if rate is None or not bucket:
                continue
            if item.get("category") == "import_usage":
                configured = self._optional_float(rates.get(bucket))
            elif item.get("category") == "export_credit":
                configured = export_rate
            else:
                configured = None
            if configured is not None and abs(configured - rate) > 0.0005:
                label = str(bucket).replace("_", " ").title()
                mismatches.append(f"{label}: bill ${rate:.6f}/kWh, configured ${configured:.6f}/kWh")
        return mismatches

    def _period_bounds(self, periods: list[dict[str, Any]]) -> tuple[Optional[date], Optional[date]]:
        starts = [self._as_date(period.get("start_date")) for period in periods if period.get("start_date")]
        ends = [self._as_date(period.get("end_date")) for period in periods if period.get("end_date")]
        starts = [value for value in starts if value]
        ends = [value for value in ends if value]
        return (min(starts) if starts else None, max(ends) if ends else None)

    def _sum_items(self, bill: dict[str, Any], category: str) -> float:
        return sum(float(item.get("amount") or 0.0) for item in bill.get("line_items") or [] if item.get("category") == category)

    def _number(self, source: Any, name: str) -> float:
        value = self._value(source, name, 0.0)
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _optional_float(self, value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _value(self, source: Any, name: str, default: Any = None) -> Any:
        if isinstance(source, dict):
            return source.get(name, default)
        return getattr(source, name, default)

    def _as_date(self, value: Any) -> Optional[date]:
        if isinstance(value, date):
            return value
        if not value:
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None

    def _iso(self, value: Any) -> Optional[str]:
        if isinstance(value, date):
            return value.isoformat()
        return str(value) if value else None
