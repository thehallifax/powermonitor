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
BATTERY_USABLE_CAPACITIES_KWH = (5.0, 10.0, 15.0, 20.0)
BATTERY_DIMINISHING_RETURN_PCT = 0.15
BATTERY_MIN_UTILISATION_PCT = 20.0
BATTERY_SAVINGS_CAPTURE_PCT = 0.90


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
        current_tariff_version: Optional[Any] = None,
        comparison_tariff_version: Optional[Any] = None,
    ) -> dict[str, Any]:
        comparison_usage_rates = self._tariff_usage_rates(comparison_tariff_version)
        flat_rate = float(
            comparison_usage_rates.get("peak")
            if comparison_usage_rates
            else comparison_plan_rates["flat_import_rate"]
        )
        supply_days = int(self._number(summary, "supply_days"))
        supply_rate = float(
            self._value(comparison_tariff_version, "supply_charge_per_day")
            if comparison_tariff_version is not None
            else comparison_plan_rates.get("supply_charge_per_day", self._number(summary, "supply_cost") / supply_days if supply_days else 0.0)
        )
        version_export_rate = self._tariff_export_rate(comparison_tariff_version)
        export_rate = float(version_export_rate if version_export_rate is not None else comparison_plan_rates.get("export_credit_rate", 0.0))

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
                "tariff_version": self._tariff_metadata(current_tariff_version),
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
                "tariff_version": self._tariff_metadata(comparison_tariff_version),
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

    def compare_home_battery_sizes(
        self,
        intervals: list[dict[str, Any]],
        settings: Any,
        *,
        baseline_bill: float,
        baseline_grid_import_kwh: float = 0.0,
        baseline_solar_exported_kwh: float = 0.0,
        capacities_kwh: tuple[float, ...] = BATTERY_USABLE_CAPACITIES_KWH,
        tariff_version: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Model self-consumption battery sizing from observed interval data.

        The simulator starts each day with an empty battery and resets at day
        end. That avoids inventing state from the day before the period and is
        intentionally conservative for partial billing periods.
        """

        normalised = [self._battery_interval(row, settings) for row in intervals]
        normalised = [row for row in normalised if row["timestamp"]]
        days = sorted({row["day"] for row in normalised})
        import_rates = self._tariff_usage_rates(tariff_version) or self._value(settings, "import_rates", {}) or {}
        known_rates = {str(bucket): float(rate or 0.0) for bucket, rate in import_rates.items()}
        cheapest_bucket = min(known_rates, key=known_rates.get) if known_rates else None
        cheapest_rate = known_rates.get(cheapest_bucket, 0.0) if cheapest_bucket else 0.0
        export_credit_rate = self._tariff_export_rate(tariff_version)
        if export_credit_rate is None:
            export_credit_rate = float(self._value(settings, "export_credit_rate", 0.0) or 0.0)
        tariff_metadata = self._tariff_metadata(tariff_version)
        baseline = self._battery_baseline(
            baseline_bill,
            baseline_grid_import_kwh,
            baseline_solar_exported_kwh,
            len(days),
        )
        options = []
        previous_savings = 0.0
        for capacity in capacities_kwh:
            option = self._simulate_battery_capacity(
                capacity,
                normalised,
                days,
                baseline_bill,
                baseline_grid_import_kwh,
                baseline_solar_exported_kwh,
                export_credit_rate,
                cheapest_rate,
            )
            option["marginal_savings"] = option["period_savings"] - previous_savings
            previous_savings = option["period_savings"]
            options.append(option)
        for index, option in enumerate(options):
            next_option = options[index + 1] if index + 1 < len(options) else None
            option["marginal_savings_to_next_capacity"] = (
                next_option["period_savings"] - option["period_savings"]
                if next_option is not None
                else None
            )

        recommendation = self._battery_sizing_recommendation(options)
        self._apply_battery_option_signals(options, recommendation)
        opportunity = self._battery_opportunity_metrics(options, normalised, days, cheapest_rate)
        diagnostics = {
            "methodology": "Daily reset: each day starts at 0 kWh state of charge and does not carry unused energy into the next day.",
            "options": [
                {
                    "capacity_kwh": option["capacity_kwh"],
                    "label": option["label"],
                    "signal": option["recommendation_signal"],
                    "summary": option["diagnostics_summary"],
                    "daily_rows": option["daily_diagnostics"],
                }
                for option in options
            ],
            "warnings": [
                warning
                for option in options
                for warning in option["diagnostics_summary"].get("warnings", [])
            ],
        }
        data_quality = {
            "intervals": len(normalised),
            "days": len(days),
            "has_verified_excess_solar": any(row["solar_export_kwh"] > 0 for row in normalised),
            "has_grid_import": any(row["grid_import_kwh"] > 0 for row in normalised),
            "confidence": "medium" if normalised else "low",
            "note": (
                "Uses actual interval imports and verified solar export."
                if normalised
                else "Battery modelling is unavailable because no interval data was supplied."
            ),
        }

        return {
            "available": bool(normalised),
            "methodology": (
                "Tariff-aware self-consumption simulation using actual interval readings. "
                "The model charges only from verified excess solar export, never from the grid, "
                "then discharges to observed grid imports by tariff value with Peak first. "
                "Each local day starts empty and resets at day end; unused stored energy is not credited. "
                "Intervals with verified export are not discharged in the same interval."
            ),
            "assumptions": {
                "usable_capacities_kwh": [float(value) for value in capacities_kwh],
                "daily_state": "starts_empty_and_resets_daily",
                "grid_charging": False,
                "charge_source": "verified_excess_solar_export",
                "round_trip_efficiency_pct": 100.0,
                "diminishing_return_rule": (
                    "Recommend the smallest usable capacity that captures at least "
                    f"{BATTERY_SAVINGS_CAPTURE_PCT:.0%} of the best observed period savings while using at least "
                    f"{BATTERY_MIN_UTILISATION_PCT:.0f}% of capacity on an average day. "
                    "Capacities beyond the point where marginal savings are below "
                    f"{BATTERY_DIMINISHING_RETURN_PCT:.0%} of the previous size's savings are treated as diminishing returns."
                ),
            },
            "tariff_context": {
                "import_rates": known_rates,
                "export_credit_rate": export_credit_rate,
                "cheapest_bucket": cheapest_bucket,
                "cheapest_rate": cheapest_rate,
                "discharge_rule": "peak_first_then_other_non_cheapest_by_rate",
            },
            "tariff_version": tariff_metadata.get("version_id"),
            "tariff_effective_from": tariff_metadata.get("effective_from"),
            "tariff_name": tariff_metadata.get("tariff_name"),
            "tariff_metadata": tariff_metadata,
            "baseline": baseline,
            "options": options,
            "recommended": recommendation,
            "opportunity_metrics": opportunity,
            "diagnostics": diagnostics,
            "data_quality": data_quality,
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

    def _battery_interval(self, row: dict[str, Any], settings: Any) -> dict[str, Any]:
        timestamp = str(row.get("timestamp") or row.get("measurement_time") or row.get("start_time") or "")
        bucket = str(row.get("tariff_bucket") or row.get("bucket") or "")
        import_rates = self._value(settings, "import_rates", {}) or {}
        rate = row.get("import_rate")
        if rate is None:
            rate = import_rates.get(bucket, 0.0) if isinstance(import_rates, dict) else 0.0
        return {
            "timestamp": timestamp,
            "day": timestamp[:10],
            "tariff_bucket": bucket,
            "rate": float(rate or 0.0),
            "grid_import_kwh": max(float(row.get("grid_import_kwh", row.get("grid_to_home_kwh", 0.0)) or 0.0), 0.0),
            "solar_export_kwh": max(float(row.get("solar_export_kwh", row.get("solar_to_grid_kwh", 0.0)) or 0.0), 0.0),
        }

    def _tariff_usage_rates(self, tariff_version: Optional[Any]) -> dict[str, float]:
        if tariff_version is None:
            return {}
        rates = self._value(tariff_version, "usage_rate_map")
        if isinstance(rates, dict):
            return {str(bucket): float(rate or 0.0) for bucket, rate in rates.items()}
        rates = self._value(tariff_version, "usage_rates", {}) or {}
        if isinstance(rates, dict):
            return {str(bucket): float(rate or 0.0) for bucket, rate in rates.items()}
        return {str(bucket): float(rate or 0.0) for bucket, rate in rates}

    def _tariff_export_rate(self, tariff_version: Optional[Any]) -> Optional[float]:
        if tariff_version is None:
            return None
        rate = self._value(tariff_version, "primary_export_rate")
        if rate is not None:
            return float(rate)
        export_rates = self._value(tariff_version, "export_rate_map")
        if isinstance(export_rates, dict) and export_rates:
            return float(export_rates.get("standard", next(iter(export_rates.values()))))
        export_rates = self._value(tariff_version, "export_rates", {}) or {}
        if isinstance(export_rates, dict) and export_rates:
            return float(export_rates.get("standard", next(iter(export_rates.values()))))
        if export_rates:
            return float(dict(export_rates).get("standard", next(iter(dict(export_rates).values()))))
        return None

    def _tariff_metadata(self, tariff_version: Optional[Any]) -> dict[str, Any]:
        if tariff_version is None:
            return {}
        public = self._value(tariff_version, "public_metadata")
        if callable(public):
            return public()
        as_dict = self._value(tariff_version, "as_dict")
        if callable(as_dict):
            data = as_dict()
        elif isinstance(tariff_version, dict):
            data = tariff_version
        else:
            data = {}
        return {
            "provider": data.get("provider") or self._value(tariff_version, "provider"),
            "tariff_name": data.get("tariff_name") or self._value(tariff_version, "tariff_name"),
            "version": data.get("version") or self._value(tariff_version, "version"),
            "version_id": data.get("version_id") or self._value(tariff_version, "version_id"),
            "effective_from": data.get("effective_from") or self._value(tariff_version, "effective_from"),
            "effective_to": data.get("effective_to") or self._value(tariff_version, "effective_to"),
            "currency": data.get("currency") or self._value(tariff_version, "currency"),
            "source_identifier": data.get("source_identifier") or self._value(tariff_version, "source_identifier"),
        }

    def _battery_baseline(
        self,
        baseline_bill: float,
        baseline_grid_import_kwh: float,
        baseline_solar_exported_kwh: float,
        days: int,
    ) -> dict[str, Any]:
        return {
            "capacity_kwh": 0.0,
            "label": "No battery",
            "estimated_bill": float(baseline_bill),
            "period_savings": 0.0,
            "annualised_savings": 0.0,
            "avoided_import_kwh": 0.0,
            "peak_import_avoided_kwh": 0.0,
            "other_import_avoided_kwh": 0.0,
            "stored_solar_kwh": 0.0,
            "discharged_kwh": 0.0,
            "average_daily_utilisation_kwh": 0.0,
            "capacity_used_pct": 0.0,
            "days_full": 0,
            "days_depleted": 0,
            "remaining_grid_import_kwh": float(baseline_grid_import_kwh),
            "remaining_solar_export_kwh": float(baseline_solar_exported_kwh),
            "marginal_savings": 0.0,
            "modelled_days": int(days),
        }

    def _simulate_battery_capacity(
        self,
        capacity_kwh: float,
        intervals: list[dict[str, Any]],
        days: list[str],
        baseline_bill: float,
        baseline_grid_import_kwh: float,
        baseline_solar_exported_kwh: float,
        export_credit_rate: float,
        cheapest_rate: float,
    ) -> dict[str, Any]:
        by_day: dict[str, list[dict[str, Any]]] = {day: [] for day in days}
        for row in intervals:
            by_day.setdefault(row["day"], []).append(row)

        stored_solar = 0.0
        discharged = 0.0
        avoided_cost = 0.0
        peak_avoided = 0.0
        other_avoided = 0.0
        days_full = 0
        days_depleted = 0
        discharge_buckets: dict[str, float] = {}
        daily_diagnostics = []

        for day, day_rows in by_day.items():
            daily_export = sum(row["solar_export_kwh"] for row in day_rows)
            daily_stored = min(float(capacity_kwh), daily_export)
            rejected_solar = max(daily_export - daily_stored, 0.0)
            remaining_state = daily_stored
            stored_solar += daily_stored
            if capacity_kwh > 0 and daily_stored >= float(capacity_kwh) - 1e-9:
                days_full += 1

            peak_before = sum(row["grid_import_kwh"] for row in day_rows if row["tariff_bucket"] == "peak")
            other_before = sum(
                row["grid_import_kwh"]
                for row in day_rows
                if row["grid_import_kwh"] > 0 and row["tariff_bucket"] != "peak" and row["rate"] > cheapest_rate + 1e-9
            )
            cheapest_before = sum(row["grid_import_kwh"] for row in day_rows if row["grid_import_kwh"] > 0 and row["rate"] <= cheapest_rate + 1e-9)
            eligible_peak_before = sum(
                row["grid_import_kwh"]
                for row in day_rows
                if row["tariff_bucket"] == "peak" and row["solar_export_kwh"] <= 1e-9
            )

            candidates = [
                row
                for row in day_rows
                if row["grid_import_kwh"] > 0
                and row["solar_export_kwh"] <= 1e-9
                and row["rate"] > cheapest_rate + 1e-9
            ]
            candidates.sort(
                key=lambda row: (
                    1 if row["tariff_bucket"] == "peak" else 0,
                    row["rate"],
                    row["timestamp"],
                ),
                reverse=True,
            )
            day_peak_discharge = 0.0
            day_other_discharge = 0.0
            day_avoided_cost = 0.0
            for row in candidates:
                if remaining_state <= 1e-9:
                    break
                amount = min(remaining_state, row["grid_import_kwh"])
                remaining_state -= amount
                discharged += amount
                avoided = amount * row["rate"]
                avoided_cost += avoided
                day_avoided_cost += avoided
                discharge_buckets[row["tariff_bucket"]] = discharge_buckets.get(row["tariff_bucket"], 0.0) + amount
                if row["tariff_bucket"] == "peak":
                    peak_avoided += amount
                    day_peak_discharge += amount
                else:
                    other_avoided += amount
                    day_other_discharge += amount

            if daily_stored > 0 and remaining_state <= 1e-9:
                days_depleted += 1

            total_day_discharge = day_peak_discharge + day_other_discharge
            remaining_peak = max(peak_before - day_peak_discharge, 0.0)
            remaining_other = max(other_before - day_other_discharge, 0.0)
            day_savings = day_avoided_cost - daily_stored * export_credit_rate
            checks = []
            if daily_stored > daily_export + 1e-9:
                checks.append("charged_more_than_verified_export")
            if total_day_discharge > daily_stored + 1e-9:
                checks.append("discharged_more_than_stored")
            if max(eligible_peak_before - day_peak_discharge, 0.0) > 1e-9 and day_other_discharge > 1e-9:
                checks.append("other_tariff_served_before_eligible_peak_import")
            if cheapest_before > 0 and total_day_discharge > peak_before + other_before + 1e-9:
                checks.append("cheapest_window_discharged")
            if any(row["solar_export_kwh"] > 1e-9 and row["grid_import_kwh"] > 0 for row in candidates):
                checks.append("simultaneous_charge_and_discharge")
            if abs((daily_stored - total_day_discharge) - remaining_state) > 1e-9:
                checks.append("daily_state_reconciliation_failed")
            if abs((daily_export - daily_stored) - rejected_solar) > 1e-9:
                checks.append("solar_export_reconciliation_failed")
            daily_diagnostics.append(
                {
                    "date": day,
                    "starting_state_of_charge_kwh": 0.0,
                    "verified_solar_export_available_kwh": daily_export,
                    "charged_into_battery_kwh": daily_stored,
                    "rejected_solar_because_battery_full_kwh": rejected_solar,
                    "peak_import_before_battery_kwh": peak_before,
                    "other_non_cheapest_import_before_battery_kwh": other_before,
                    "cheapest_window_import_before_battery_kwh": cheapest_before,
                    "battery_discharge_into_peak_kwh": day_peak_discharge,
                    "battery_discharge_into_other_non_cheapest_kwh": day_other_discharge,
                    "total_battery_discharge_kwh": total_day_discharge,
                    "ending_state_of_charge_kwh": remaining_state,
                    "remaining_peak_import_kwh": remaining_peak,
                    "remaining_other_non_cheapest_import_kwh": remaining_other,
                    "remaining_cheapest_window_import_kwh": cheapest_before,
                    "remaining_solar_export_kwh": rejected_solar,
                    "estimated_savings": day_savings,
                    "energy_conservation_check": "pass" if not checks else "fail",
                    "warnings": checks,
                }
            )

        lost_export_credit = stored_solar * export_credit_rate
        savings = avoided_cost - lost_export_credit
        estimated_bill = float(baseline_bill) - savings
        modelled_days = len(days)
        average_daily_utilisation = discharged / modelled_days if modelled_days else 0.0
        capacity_used_pct = average_daily_utilisation / float(capacity_kwh) * 100 if capacity_kwh else 0.0
        annualised = self.projected_annual_cost(savings, modelled_days) if modelled_days else 0.0
        diagnostics_summary = self._battery_diagnostics_summary(daily_diagnostics)

        return {
            "capacity_kwh": float(capacity_kwh),
            "label": f"{capacity_kwh:g} kWh usable",
            "estimated_bill": estimated_bill,
            "period_savings": savings,
            "annualised_savings": annualised,
            "avoided_import_kwh": discharged,
            "avoided_import_cost": avoided_cost,
            "lost_export_credit": lost_export_credit,
            "peak_import_avoided_kwh": peak_avoided,
            "other_import_avoided_kwh": other_avoided,
            "stored_solar_kwh": stored_solar,
            "discharged_kwh": discharged,
            "average_daily_utilisation_kwh": average_daily_utilisation,
            "capacity_used_pct": capacity_used_pct,
            "days_full": days_full,
            "days_depleted": days_depleted,
            "remaining_grid_import_kwh": max(float(baseline_grid_import_kwh) - discharged, 0.0),
            "remaining_solar_export_kwh": max(float(baseline_solar_exported_kwh) - stored_solar, 0.0),
            "discharge_buckets": discharge_buckets,
            "modelled_days": modelled_days,
            "daily_diagnostics": daily_diagnostics,
            "diagnostics_summary": diagnostics_summary,
        }

    def _battery_diagnostics_summary(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        fields = (
            "verified_solar_export_available_kwh",
            "charged_into_battery_kwh",
            "rejected_solar_because_battery_full_kwh",
            "peak_import_before_battery_kwh",
            "other_non_cheapest_import_before_battery_kwh",
            "cheapest_window_import_before_battery_kwh",
            "battery_discharge_into_peak_kwh",
            "battery_discharge_into_other_non_cheapest_kwh",
            "total_battery_discharge_kwh",
            "ending_state_of_charge_kwh",
            "remaining_peak_import_kwh",
            "remaining_other_non_cheapest_import_kwh",
            "remaining_cheapest_window_import_kwh",
            "remaining_solar_export_kwh",
            "estimated_savings",
        )
        warnings = [
            f"{row['date']}: {warning}"
            for row in rows
            for warning in row.get("warnings", [])
        ]
        return {
            "days": len(rows),
            "totals": {field: sum(float(row.get(field) or 0.0) for row in rows) for field in fields},
            "warnings": warnings,
            "energy_conservation_check": "pass" if not warnings else "fail",
        }

    def _battery_opportunity_metrics(
        self,
        options: list[dict[str, Any]],
        intervals: list[dict[str, Any]],
        days: list[str],
        cheapest_rate: float,
    ) -> dict[str, Any]:
        day_count = len(days)
        available_export = sum(row["solar_export_kwh"] for row in intervals)
        non_cheapest_import = sum(
            row["grid_import_kwh"]
            for row in intervals
            if row["grid_import_kwh"] > 0 and row["rate"] > cheapest_rate + 1e-9
        )
        recommended = next((option for option in options if option.get("recommendation_signal") == "Recommended"), options[0] if options else {})
        return {
            "average_daily_solar_export_available_kwh": available_export / day_count if day_count else 0.0,
            "average_daily_non_cheapest_grid_import_kwh": non_cheapest_import / day_count if day_count else 0.0,
            "average_daily_battery_energy_charged_kwh": float(recommended.get("stored_solar_kwh") or 0.0) / day_count if day_count else 0.0,
            "average_daily_battery_energy_discharged_kwh": float(recommended.get("discharged_kwh") or 0.0) / day_count if day_count else 0.0,
            "average_battery_utilisation_pct": float(recommended.get("capacity_used_pct") or 0.0),
            "days_battery_reached_full_charge": int(recommended.get("days_full") or 0),
            "days_battery_fully_depleted": int(recommended.get("days_depleted") or 0),
            "remaining_solar_export_kwh": float(recommended.get("remaining_solar_export_kwh") or 0.0),
            "marginal_savings_to_next_capacity": recommended.get("marginal_savings_to_next_capacity"),
        }

    def _apply_battery_option_signals(self, options: list[dict[str, Any]], recommendation: dict[str, Any]) -> None:
        recommended_capacity = float(recommendation.get("capacity_kwh") or 0.0)
        for option in options:
            capacity = float(option.get("capacity_kwh") or 0.0)
            savings = float(option.get("period_savings") or 0.0)
            next_marginal = option.get("marginal_savings_to_next_capacity")
            utilisation = float(option.get("capacity_used_pct") or 0.0)
            if abs(capacity - recommended_capacity) < 1e-9 and recommendation.get("status") == "recommended":
                signal = "Recommended"
            elif savings <= 0.005:
                signal = "Limited additional benefit"
            elif capacity < recommended_capacity:
                signal = "Good value"
            elif next_marginal is None and capacity > recommended_capacity:
                signal = "Likely oversized for this usage pattern"
            elif capacity > recommended_capacity and (
                utilisation < BATTERY_MIN_UTILISATION_PCT
            ):
                signal = "Likely oversized for this usage pattern"
            elif capacity > recommended_capacity:
                signal = "Limited additional benefit"
            else:
                signal = "Good value"
            option["recommendation_signal"] = signal

    def _battery_sizing_recommendation(self, options: list[dict[str, Any]]) -> dict[str, Any]:
        if not options:
            return {
                "capacity_kwh": 0.0,
                "status": "insufficient_data",
                "summary": "Battery sizing is unavailable because interval data is missing.",
                "diminishing_return_capacity_kwh": None,
                "diminishing_return_reason": "No modelled battery options.",
            }

        best_savings = max(option["period_savings"] for option in options)
        if best_savings <= 0.005:
            return {
                "capacity_kwh": 0.0,
                "status": "no_financial_case",
                "summary": "This period does not show useful battery savings from verified excess solar.",
                "diminishing_return_capacity_kwh": options[0]["capacity_kwh"],
                "diminishing_return_reason": "Modelled savings are zero or negative after lost export credits.",
            }

        target_savings = best_savings * BATTERY_SAVINGS_CAPTURE_PCT
        selected = None
        for option in options:
            if option["period_savings"] >= target_savings and option["capacity_used_pct"] >= BATTERY_MIN_UTILISATION_PCT:
                selected = option
                break
        if selected is None:
            selected = max(options, key=lambda option: option["period_savings"])

        diminishing_option = None
        previous = None
        for option in options:
            if previous is not None:
                threshold = max(previous["period_savings"], 0.0) * BATTERY_DIMINISHING_RETURN_PCT
                if option["marginal_savings"] <= threshold or option["capacity_used_pct"] < BATTERY_MIN_UTILISATION_PCT:
                    diminishing_option = option
                    break
            previous = option

        extra_note = ""
        if diminishing_option and diminishing_option["capacity_kwh"] > selected["capacity_kwh"]:
            extra_note = (
                f" The {diminishing_option['capacity_kwh']:g} kWh step is where returns start to flatten, "
                f"adding only ${max(diminishing_option['marginal_savings'], 0.0):.2f} in this period."
            )

        return {
            "capacity_kwh": selected["capacity_kwh"],
            "status": "recommended",
            "summary": (
                f"Based on this period, {selected['capacity_kwh']:g} kWh usable storage captures most of the observed "
                f"battery value, saving about ${selected['period_savings']:.2f} for the period "
                f"(${selected['annualised_savings']:.0f} annualised on the same pattern)."
                f"{extra_note}"
            ),
            "period_savings": selected["period_savings"],
            "annualised_savings": selected["annualised_savings"],
            "diminishing_return_capacity_kwh": diminishing_option["capacity_kwh"] if diminishing_option else None,
            "diminishing_return_reason": (
                "Marginal savings fall below the configured threshold or average utilisation falls below the minimum."
                if diminishing_option
                else "No tested capacity crossed the diminishing-return threshold."
            ),
        }

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
