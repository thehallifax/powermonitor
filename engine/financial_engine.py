#!/usr/bin/env python3
"""Financial impact calculations for PowerMonitor.

The Financial Engine is the single owner for tariff, bill, savings, projection,
and comparison arithmetic. It works from already-normalised platform data and
does not query SQLite, call connectors, render UI, or generate recommendations.
"""

from datetime import date, timedelta
from typing import Any, Optional, Union

from .models import EngineStatus, FinancialImpact, HouseholdContext

BUCKET_ORDER = ("overnight", "off_peak", "super_off_peak", "peak")


class FinancialEngine:
    """Tariff-aware financial impact calculations."""

    name = "financial_engine"

    def __init__(self, household_context: Optional[HouseholdContext] = None) -> None:
        self.household_context = household_context

    def status(self) -> EngineStatus:
        return EngineStatus(name=self.name)

    def evaluate(self, context: dict) -> list[FinancialImpact]:
        return []

    def financial_impact(
        self,
        today: Optional[float] = None,
        billing_period: Optional[float] = None,
        yearly: Optional[float] = None,
        basis: Optional[str] = None,
    ) -> FinancialImpact:
        return FinancialImpact(
            today=today,
            billing_period=billing_period,
            yearly=yearly,
            basis=basis,
        )

    def calculate_import_cost(
        self,
        kwh: float,
        rate_or_rates: Union[float, dict[str, float]],
        bucket: Optional[str] = None,
    ) -> float:
        rate = rate_or_rates[bucket] if bucket is not None and isinstance(rate_or_rates, dict) else rate_or_rates
        return float(kwh) * float(rate)

    def calculate_export_credit(self, exported_kwh: float, export_credit_rate: float) -> float:
        return float(exported_kwh) * float(export_credit_rate)

    def calculate_net_bill(
        self,
        import_cost: float,
        supply_cost: float,
        export_credit: float,
    ) -> float:
        return float(import_cost) + float(supply_cost) - float(export_credit)

    def cost_of_ev_session(
        self,
        energy_kwh: float,
        rate: float,
    ) -> float:
        return self.calculate_import_cost(energy_kwh, rate)

    def calculate_load_shift_savings(
        self,
        kwh: float,
        from_rate: float,
        to_rate: float,
    ) -> float:
        return float(kwh) * (float(from_rate) - float(to_rate))

    def calculate_cost_difference(self, current_cost: float, comparison_cost: float) -> float:
        return float(current_cost) - float(comparison_cost)

    def weighted_average_import_rate(self, summary: Any) -> float:
        import_cost = self._number(summary, "import_cost")
        grid_import_kwh = self._number(summary, "grid_import_kwh")
        return import_cost / grid_import_kwh if grid_import_kwh else 0.0

    def projected_monthly_cost(self, cost: float, days: int, month_days: int = 30) -> float:
        return float(cost) / int(days) * month_days if days else float(cost)

    def projected_annual_cost(self, cost: float, days: int, year_days: int = 365) -> float:
        return float(cost) / int(days) * year_days if days else float(cost)

    def project_bill(self, summary: Any, settings: Any) -> tuple[float, date, str, FinancialImpact]:
        if self._value(settings, "current_billing_period_start") and self._value(settings, "current_billing_period_end"):
            end = self._as_date(self._value(settings, "current_billing_period_end"))
            start = self._as_date(self._value(settings, "current_billing_period_start"))
            days = (end - start).days + 1
            basis = "configured billing end"
        elif (
            self._value(settings, "current_billing_period_start")
            and self._value(settings, "previous_billing_period_start")
            and self._value(settings, "previous_billing_period_end")
        ):
            current_start = self._as_date(self._value(settings, "current_billing_period_start"))
            previous_start = self._as_date(self._value(settings, "previous_billing_period_start"))
            previous_end = self._as_date(self._value(settings, "previous_billing_period_end"))
            days = (previous_end - previous_start).days + 1
            end = current_start + timedelta(days=days - 1)
            basis = "previous period length"
        else:
            days = int(self._number(summary, "supply_days"))
            end = self._as_date(self._value(summary, "end"))
            basis = "selected period"

        projected = self._number(summary, "average_daily_net_cost") * max(days, 1)
        return projected, end, basis, self.financial_impact(billing_period=projected, basis=basis)

    def compare_tariff_plans(
        self,
        summary: Any,
        current_plan_name: str,
        comparison_plan_name: str,
        comparison_plan_rates: dict[str, float],
        current_display_name: str = "Synergy EV Add On",
        comparison_display_name: str = "Previous Flat/Home Plan",
    ) -> dict[str, Any]:
        flat_rate = float(comparison_plan_rates["flat_import_rate"])
        supply_days = int(self._number(summary, "supply_days"))
        supply_rate = float(comparison_plan_rates.get("supply_charge_per_day", self._number(summary, "supply_cost") / supply_days if supply_days else 0.0))
        export_rate = float(comparison_plan_rates.get("export_credit_rate", 0.0))

        grid_import_kwh = self._number(summary, "grid_import_kwh")
        solar_exported_kwh = self._number(summary, "solar_exported_kwh")
        ev_charging_kwh = self._number(summary, "ev_charging_kwh")
        current_import_cost = self._number(summary, "import_cost")
        current_supply_cost = self._number(summary, "supply_cost")
        current_export_credit = self._number(summary, "export_credit")
        current_net_bill = self._number(summary, "net_cost")
        current_ev_cost = self._number(summary, "ev_charging_cost")

        comparison_import_cost = self.calculate_import_cost(grid_import_kwh, flat_rate)
        comparison_supply_charge = supply_days * supply_rate
        comparison_export_credit = self.calculate_export_credit(solar_exported_kwh, export_rate)
        comparison_net_bill = self.calculate_net_bill(
            comparison_import_cost,
            comparison_supply_charge,
            comparison_export_credit,
        )
        comparison_ev_cost = self.cost_of_ev_session(ev_charging_kwh, flat_rate)
        current_household_cost = max(current_import_cost - current_ev_cost, 0.0)
        comparison_household_kwh = max(grid_import_kwh - ev_charging_kwh, 0.0)
        comparison_household_cost = self.calculate_import_cost(comparison_household_kwh, flat_rate)
        export_value_difference = current_export_credit - comparison_export_credit
        savings = comparison_net_bill - current_net_bill
        monthly_savings = self.projected_monthly_cost(savings, supply_days)

        recommendation = (
            f"The EV Add On is currently saving approximately ${monthly_savings:,.2f} per month."
            if savings > 0
            else "Based on current charging behaviour, the Flat plan would currently be cheaper."
            if savings < 0
            else "The EV Add On and Flat plan are currently about even for this period."
        )

        return {
            "current_plan": {
                "name": current_plan_name,
                "display_name": current_display_name,
                "grid_import_cost": current_import_cost,
                "supply_charge": current_supply_cost,
                "export_credit": current_export_credit,
                "net_bill": current_net_bill,
                "ev_charging_cost": current_ev_cost,
                "household_usage_cost": current_household_cost,
            },
            "comparison_plan": {
                "name": comparison_plan_name,
                "display_name": comparison_display_name,
                "flat_import_rate": flat_rate,
                "grid_import_cost": comparison_import_cost,
                "supply_charge": comparison_supply_charge,
                "export_credit": comparison_export_credit,
                "net_bill": comparison_net_bill,
                "ev_charging_cost": comparison_ev_cost,
                "household_usage_cost": comparison_household_cost,
            },
            "difference": current_net_bill - comparison_net_bill,
            "savings": savings,
            "monthly_savings_estimate": monthly_savings,
            "ev_charging_cost_difference": current_ev_cost - comparison_ev_cost,
            "household_usage_cost_difference": current_household_cost - comparison_household_cost,
            "solar_export_value_difference": export_value_difference,
            "recommendation": recommendation,
        }

    def _value(self, source: Any, name: str, default: Any = None) -> Any:
        if isinstance(source, dict):
            return source.get(name, default)
        return getattr(source, name, default)

    def _number(self, source: Any, name: str, default: float = 0.0) -> float:
        value = self._value(source, name, default)
        return float(value or 0.0)

    def _as_date(self, value: Any) -> date:
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))
