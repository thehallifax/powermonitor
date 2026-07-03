#!/usr/bin/env python3
"""Local Australia-first tariff setup catalog.

This catalog is intentionally static. FIN-001 does not scrape live tariffs or
perform AI tariff discovery; unsupported retailers and plans use manual entry.
"""

from dataclasses import dataclass
from typing import Any, Optional

BUCKET_ORDER = ("overnight", "off_peak", "super_off_peak", "peak")
MANUAL_TARIFF_ID = "manual_tariff"
MANUAL_TARIFF_LABEL = "Manual tariff"
DEFAULT_STATE_OR_TERRITORY = "WA"
DEFAULT_RETAILER = "synergy"
DEFAULT_PLAN = "synergy_ev_add_on"
NO_ADD_ON = "none"
EV_ADD_ON = "ev_add_on"


@dataclass(frozen=True)
class TariffPlan:
    plan_id: str
    label: str
    state_or_territory: str
    retailer_id: str
    retailer_label: str
    import_rates: dict[str, float]
    supply_charge_per_day: float
    export_credit_rate: float
    add_on_id: str = NO_ADD_ON
    add_on_label: str = "None"


SYNERGY_EV_RATES = {
    "super_off_peak": 0.086151,
    "overnight": 0.193841,
    "off_peak": 0.236916,
    "peak": 0.538446,
}
FLAT_HOME_RATES = {
    "super_off_peak": 0.30,
    "overnight": 0.30,
    "off_peak": 0.30,
    "peak": 0.30,
}
DEFAULT_SUPPLY_CHARGE_PER_DAY = 1.292269
DEFAULT_EXPORT_CREDIT_RATE = 0.02

SUPPORTED_PLANS: dict[str, TariffPlan] = {
    "synergy_home_plan": TariffPlan(
        plan_id="synergy_home_plan",
        label="Synergy Home Plan",
        state_or_territory="WA",
        retailer_id="synergy",
        retailer_label="Synergy",
        import_rates=FLAT_HOME_RATES,
        supply_charge_per_day=DEFAULT_SUPPLY_CHARGE_PER_DAY,
        export_credit_rate=DEFAULT_EXPORT_CREDIT_RATE,
    ),
    "synergy_ev_add_on": TariffPlan(
        plan_id="synergy_ev_add_on",
        label="Synergy EV Add On",
        state_or_territory="WA",
        retailer_id="synergy",
        retailer_label="Synergy",
        import_rates=SYNERGY_EV_RATES,
        supply_charge_per_day=DEFAULT_SUPPLY_CHARGE_PER_DAY,
        export_credit_rate=DEFAULT_EXPORT_CREDIT_RATE,
        add_on_id=EV_ADD_ON,
        add_on_label="EV Add On",
    ),
    "previous_flat_or_home_plan": TariffPlan(
        plan_id="previous_flat_or_home_plan",
        label="Previous Flat/Home Plan",
        state_or_territory="WA",
        retailer_id="synergy",
        retailer_label="Synergy",
        import_rates=FLAT_HOME_RATES,
        supply_charge_per_day=DEFAULT_SUPPLY_CHARGE_PER_DAY,
        export_credit_rate=DEFAULT_EXPORT_CREDIT_RATE,
    ),
}

STATE_OPTIONS = (("WA", "Western Australia"),)
RETAILER_OPTIONS = (("synergy", "Synergy"),)
ADD_ON_OPTIONS = ((NO_ADD_ON, "None"), (EV_ADD_ON, "EV Add On"))


def supported_plan_options() -> tuple[tuple[str, str], ...]:
    return tuple((plan.plan_id, plan.label) for plan in SUPPORTED_PLANS.values())


def plan_label(plan_id: str) -> str:
    if plan_id == MANUAL_TARIFF_ID:
        return MANUAL_TARIFF_LABEL
    plan = SUPPORTED_PLANS.get(plan_id)
    return plan.label if plan else str(plan_id or "")


def tariff_plan_for_selection(
    state_or_territory: str,
    retailer: str,
    plan_id: str,
    add_on_id: str = NO_ADD_ON,
) -> Optional[TariffPlan]:
    state = str(state_or_territory or "").upper()
    retailer_id = str(retailer or "").lower()
    plan = str(plan_id or "")
    add_on = str(add_on_id or NO_ADD_ON)
    if plan == "synergy_home_plan" and add_on == EV_ADD_ON:
        plan = "synergy_ev_add_on"
    candidate = SUPPORTED_PLANS.get(plan)
    if not candidate:
        return None
    if candidate.state_or_territory != state or candidate.retailer_id != retailer_id:
        return None
    return candidate


def apply_structured_tariff(raw: dict[str, Any]) -> dict[str, Any]:
    mode = str(raw.get("tariff_setup_mode") or "catalog")
    plan_id = str(raw.get("current_plan") or DEFAULT_PLAN)
    retailer = str(raw.get("retailer") or DEFAULT_RETAILER)
    state = str(raw.get("state_or_territory") or DEFAULT_STATE_OR_TERRITORY).upper()
    add_on = str(raw.get("tariff_add_on") or NO_ADD_ON)

    if mode == "manual" or plan_id == MANUAL_TARIFF_ID or retailer == MANUAL_TARIFF_ID:
        raw["tariff_setup_mode"] = "manual"
        raw["current_plan"] = MANUAL_TARIFF_ID
        raw["retailer"] = MANUAL_TARIFF_LABEL
        raw["tariff_add_on"] = NO_ADD_ON
        raw["state_or_territory"] = state or DEFAULT_STATE_OR_TERRITORY
        return raw

    selected = tariff_plan_for_selection(state, retailer, plan_id, add_on)
    if not selected:
        return raw

    raw["tariff_setup_mode"] = "catalog"
    raw["state_or_territory"] = selected.state_or_territory
    raw["retailer"] = selected.retailer_label
    raw["retailer_id"] = selected.retailer_id
    raw["current_plan"] = selected.plan_id
    raw["tariff_add_on"] = selected.add_on_id
    raw["supply_charge_per_day"] = selected.supply_charge_per_day
    raw["import_rates"] = dict(selected.import_rates)
    raw["export_credit_rate"] = selected.export_credit_rate
    return raw


def comparison_rates_for_plan(plan_id: str) -> dict[str, float]:
    plan = SUPPORTED_PLANS.get(plan_id) or SUPPORTED_PLANS["previous_flat_or_home_plan"]
    flat_rate = plan.import_rates["peak"]
    return {
        "flat_import_rate": flat_rate,
        "supply_charge_per_day": plan.supply_charge_per_day,
        "export_credit_rate": plan.export_credit_rate,
    }
