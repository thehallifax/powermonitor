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

    def calculate_bill_variance(self, actual_cost: float, estimated_cost: float) -> float:
        return float(actual_cost) - float(estimated_cost)

    def calculate_percentage_variance(self, dollar_variance: float, actual_cost: float) -> float:
        return float(dollar_variance) / float(actual_cost) * 100 if float(actual_cost) else 0.0

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

    def explain_bill_change(
        self,
        current_summary: Any,
        previous_summary: Optional[Any],
        settings: Any,
        *,
        current_bill: Optional[float] = None,
        previous_bill: Optional[float] = None,
        projected: bool = False,
    ) -> dict[str, Any]:
        if previous_summary is None:
            return {
                "available": False,
                "reason": "Historical comparison is unavailable.",
                "narrative": "PowerMonitor cannot explain the bill change yet because a previous completed billing period is unavailable.",
                "current_bill": current_bill if current_bill is not None else self._number(current_summary, "net_cost"),
                "previous_bill": None,
                "difference": None,
                "contributors": [],
                "positive_changes": [],
                "negative_changes": [],
                "ev_explanation": None,
                "confidence": "Low",
            }

        current_total = float(current_bill if current_bill is not None else self._number(current_summary, "net_cost"))
        previous_total = float(previous_bill if previous_bill is not None else self._number(previous_summary, "net_cost"))
        difference = current_total - previous_total
        scale = self._projection_scale(current_summary, current_total)
        previous_scale = 1.0
        contributors = self._bill_change_contributors(current_summary, previous_summary, settings, scale, previous_scale)
        positive = [item for item in contributors if item["impact"] < 0]
        negative = [item for item in contributors if item["impact"] > 0]
        ev_explanation = self.ev_bill_explanation(current_summary, settings, scale=scale)
        confidence = self._bill_change_confidence(current_summary, previous_summary, projected)
        narrative = self.bill_change_narrative(
            current_total,
            previous_total,
            contributors,
            projected=projected,
            confidence=confidence,
            ev_explanation=ev_explanation,
        )
        return {
            "available": True,
            "reason": None,
            "narrative": narrative,
            "current_bill": current_total,
            "previous_bill": previous_total,
            "difference": difference,
            "contributors": contributors,
            "positive_changes": positive,
            "negative_changes": negative,
            "ev_explanation": ev_explanation,
            "confidence": confidence,
        }

    def bill_change_narrative(
        self,
        current_bill: float,
        previous_bill: float,
        contributors: list[dict[str, Any]],
        *,
        projected: bool = False,
        confidence: str = "Medium",
        ev_explanation: Optional[dict[str, Any]] = None,
    ) -> str:
        difference = current_bill - previous_bill
        bill_word = "projected to be" if projected else "about"
        direction = "higher" if difference > 0 else "lower" if difference < 0 else "about the same"
        if abs(difference) < 0.005:
            return f"Your bill is {bill_word} about the same as the previous billing period. {confidence} confidence."

        largest = [item for item in contributors if abs(float(item.get("impact") or 0.0)) > 0.005][:2]
        if largest:
            reasons = self._join_phrases([str(item["label"]).lower() for item in largest])
        else:
            reasons = "the available billing components moved slightly"
        sentence = (
            f"Your bill is {bill_word} ${abs(difference):.0f} {direction} than the previous billing period "
            f"because {reasons}."
        )
        if ev_explanation and ev_explanation.get("savings_vs_peak", 0) > 0:
            sentence += (
                f" Off-peak charging has saved approximately ${ev_explanation['savings_vs_peak']:.0f} "
                "compared with charging during peak periods."
            )
        sentence += f" {confidence} confidence."
        return sentence

    def ev_bill_explanation(self, summary: Any, settings: Any, *, scale: float = 1.0) -> Optional[dict[str, Any]]:
        source = str(self._value(summary, "ev_charging_source", "") or "")
        if source not in {"ocular_export", "home_assistant_ocpp"}:
            return None
        energy = self._number(summary, "actual_ev_charging_kwh") or self._number(summary, "ev_charging_kwh")
        cost = self._number(summary, "actual_ev_charging_cost") or self._number(summary, "ev_charging_cost")
        energy *= scale
        cost *= scale
        import_rates = self._value(settings, "import_rates", {}) or {}
        peak_rate = float(import_rates.get("peak", 0.0) or 0.0)
        peak_cost = energy * peak_rate
        savings_vs_peak = max(peak_cost - cost, 0.0)
        petrol_equivalent = self._petrol_equivalent_cost(summary, settings, energy)
        return {
            "available": True,
            "source": source,
            "total_energy_kwh": energy,
            "charging_cost": cost,
            "cost_if_all_peak": peak_cost,
            "savings_vs_peak": savings_vs_peak,
            "petrol_equivalent_cost": petrol_equivalent,
        }

    def _bill_change_contributors(
        self,
        current: Any,
        previous: Any,
        settings: Any,
        current_scale: float,
        previous_scale: float,
    ) -> list[dict[str, Any]]:
        items = []

        def add(label: str, impact: float, detail: str, category: str) -> None:
            if abs(impact) < 0.005:
                return
            items.append(
                {
                    "label": label,
                    "impact": impact,
                    "direction": "extra_cost" if impact > 0 else "saving",
                    "detail": detail,
                    "category": category,
                }
            )

        current_ev_cost = self._number(current, "ev_charging_cost") * current_scale
        previous_ev_cost = self._number(previous, "ev_charging_cost") * previous_scale
        if self._has_ev_data(current) or self._has_ev_data(previous):
            current_ev_kwh = self._number(current, "ev_charging_kwh") * current_scale
            previous_ev_kwh = self._number(previous, "ev_charging_kwh") * previous_scale
            add(
                "EV charging increased" if current_ev_cost >= previous_ev_cost else "EV charging decreased",
                current_ev_cost - previous_ev_cost,
                f"EV charging moved from {previous_ev_kwh:.1f} kWh to {current_ev_kwh:.1f} kWh.",
                "ev",
            )

        for bucket, label in (
            ("peak", "Peak usage"),
            ("off_peak", "Off-peak usage"),
            ("overnight", "Overnight usage"),
            ("super_off_peak", "Super off-peak / EV window usage"),
        ):
            current_cost = self._bucket_number(current, bucket, "cost") * current_scale
            previous_cost = self._bucket_number(previous, bucket, "cost") * previous_scale
            add(
                f"{label} increased" if current_cost >= previous_cost else f"{label} decreased",
                current_cost - previous_cost,
                f"{label} cost moved from ${previous_cost:.2f} to ${current_cost:.2f}.",
                bucket,
            )

        current_export = self._number(current, "export_credit") * current_scale
        previous_export = self._number(previous, "export_credit") * previous_scale
        add(
            "Export credits decreased" if current_export <= previous_export else "Export credits increased",
            previous_export - current_export,
            f"Export credits moved from ${previous_export:.2f} to ${current_export:.2f}.",
            "export_credit",
        )

        current_supply = self._number(current, "supply_cost") * current_scale
        previous_supply = self._number(previous, "supply_cost") * previous_scale
        add(
            "Supply charge increased" if current_supply >= previous_supply else "Supply charge decreased",
            current_supply - previous_supply,
            f"Supply charges moved from ${previous_supply:.2f} to ${current_supply:.2f}.",
            "supply",
        )

        current_solar = self._number(current, "solar_generated_kwh") * current_scale
        previous_solar = self._number(previous, "solar_generated_kwh") * previous_scale
        average_rate = self.weighted_average_import_rate(current) or self.weighted_average_import_rate(previous)
        solar_impact = (previous_solar - current_solar) * average_rate
        add(
            "Solar generation decreased" if current_solar <= previous_solar else "Solar generation increased",
            solar_impact,
            f"Solar generation moved from {previous_solar:.1f} kWh to {current_solar:.1f} kWh.",
            "solar",
        )

        current_household = max(self._number(current, "import_cost") * current_scale - current_ev_cost, 0.0)
        previous_household = max(self._number(previous, "import_cost") * previous_scale - previous_ev_cost, 0.0)
        add(
            "General household consumption increased" if current_household >= previous_household else "General household consumption decreased",
            current_household - previous_household,
            "Grid import cost after removing identifiable EV charging changed.",
            "household",
        )

        current_plan = self._value(current, "current_plan") or self._value(current, "plan_name")
        previous_plan = self._value(previous, "current_plan") or self._value(previous, "plan_name")
        if current_plan and previous_plan and current_plan != previous_plan:
            current_avg_rate = self.weighted_average_import_rate(current)
            previous_avg_rate = self.weighted_average_import_rate(previous)
            import_kwh = self._number(current, "grid_import_kwh") * current_scale
            add(
                "Tariff changes increased costs" if current_avg_rate >= previous_avg_rate else "Tariff changes reduced costs",
                (current_avg_rate - previous_avg_rate) * import_kwh,
                f"Average import rate moved from ${previous_avg_rate:.4f}/kWh to ${current_avg_rate:.4f}/kWh.",
                "tariff",
            )

        return sorted(items, key=lambda item: abs(float(item["impact"])), reverse=True)

    def _projection_scale(self, summary: Any, current_bill: float) -> float:
        net_cost = self._number(summary, "net_cost")
        if net_cost <= 0:
            return 1.0
        return max(float(current_bill) / net_cost, 0.0)

    def _bill_change_confidence(self, current: Any, previous: Any, projected: bool) -> str:
        current_days = self._number(current, "reading_days")
        previous_days = self._number(previous, "reading_days")
        if current_days < 5 or previous_days < 5:
            return "Low"
        if projected or current_days < 15:
            return "Medium"
        return "High"

    def _has_ev_data(self, summary: Any) -> bool:
        return str(self._value(summary, "ev_charging_source", "") or "") in {"ocular_export", "home_assistant_ocpp"}

    def _bucket_number(self, summary: Any, bucket: str, field: str) -> float:
        buckets = self._value(summary, "buckets", {}) or {}
        values = buckets.get(bucket, {}) if isinstance(buckets, dict) else {}
        if isinstance(values, dict):
            return float(values.get(field) or 0.0)
        return 0.0

    def _petrol_equivalent_cost(self, summary: Any, settings: Any, ev_energy_kwh: float) -> Optional[float]:
        ev_efficiency = self._value(settings, "ev_kwh_per_100km")
        petrol_l_per_100km = self._value(settings, "previous_car_l_per_100km")
        petrol_price = self._value(settings, "petrol_price_per_litre")
        try:
            ev_efficiency = float(ev_efficiency)
            petrol_l_per_100km = float(petrol_l_per_100km)
            petrol_price = float(petrol_price)
        except (TypeError, ValueError):
            return None
        if ev_efficiency <= 0 or petrol_l_per_100km <= 0 or petrol_price < 0:
            return None
        distance_km = ev_energy_kwh / ev_efficiency * 100
        return distance_km / 100 * petrol_l_per_100km * petrol_price

    def _join_phrases(self, phrases: list[str]) -> str:
        if not phrases:
            return ""
        if len(phrases) == 1:
            return phrases[0]
        return ", ".join(phrases[:-1]) + f" and {phrases[-1]}"

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
