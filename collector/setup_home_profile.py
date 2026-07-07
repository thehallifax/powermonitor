#!/usr/bin/env python3
"""CLI setup wizard for the provider-independent Home Profile."""

import sys
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.home_profile import DEFAULT_HOME_PROFILE_PATH, HomeProfile, load_home_profile, save_home_profile

InputFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]


def run_home_profile_wizard(
    path: Path = DEFAULT_HOME_PROFILE_PATH,
    *,
    input_func: InputFunc = input,
    output_func: OutputFunc = print,
) -> HomeProfile:
    """Create or update the local Home Profile using interactive prompts."""

    path = Path(path)
    existing = load_home_profile(path)
    output_func("PowerMonitor Home Profile setup")
    output_func("Press Enter to keep a shown default. Optional fields can be left blank.")

    location_region = prompt_text("Location/state", existing.location_region, input_func)
    electricity_retailer = prompt_text(
        "Electricity retailer",
        existing.electricity_retailer,
        input_func,
    )
    current_tariff = prompt_text("Current tariff", existing.current_tariff, input_func)

    solar_installed = prompt_bool("Solar installed", existing.solar_installed, input_func, output_func)
    solar_capacity_kw = None
    inverter_brand = ""
    inverter_model = ""
    if solar_installed:
        solar_capacity_kw = prompt_optional_float(
            "Solar capacity kW",
            existing.solar_capacity_kw,
            input_func,
            output_func,
        )
        inverter_brand = prompt_text("Inverter brand", existing.inverter_brand, input_func)
        inverter_model = prompt_text("Inverter model", existing.inverter_model, input_func)

    battery_installed = prompt_bool("Battery installed", existing.battery_installed, input_func, output_func)
    battery_capacity_kwh = None
    if battery_installed:
        battery_capacity_kwh = prompt_optional_float(
            "Battery capacity kWh",
            existing.battery_capacity_kwh,
            input_func,
            output_func,
        )

    ev_owned = prompt_bool("EV owned", existing.ev_owned, input_func, output_func)
    vehicle_make = ""
    vehicle_model = ""
    charger_type = ""
    if ev_owned:
        vehicle_make = prompt_text("Vehicle make", existing.vehicle_make, input_func)
        vehicle_model = prompt_text("Vehicle model", existing.vehicle_model, input_func)
        charger_type = prompt_text("Charger type", existing.charger_type, input_func)

    profile = replace(
        existing,
        location_region=location_region,
        electricity_retailer=electricity_retailer,
        current_tariff=current_tariff,
        solar_installed=solar_installed,
        solar_capacity_kw=solar_capacity_kw,
        inverter_brand=inverter_brand,
        inverter_model=inverter_model,
        battery_installed=battery_installed,
        battery_capacity_kwh=battery_capacity_kwh,
        ev_owned=ev_owned,
        vehicle_make=vehicle_make,
        vehicle_model=vehicle_model,
        charger_type=charger_type,
        household_monitoring_available=prompt_bool(
            "Household monitoring available",
            existing.household_monitoring_available,
            input_func,
            output_func,
        ),
        home_assistant_available=prompt_bool(
            "Home Assistant available",
            existing.home_assistant_available,
            input_func,
            output_func,
        ),
    )
    save_home_profile(profile, path)
    output_func(f"Home Profile saved to {path}")
    return profile


def prompt_text(label: str, default: str, input_func: InputFunc) -> str:
    response = input_func(prompt(label, default)).strip()
    return response if response else default


def prompt_bool(
    label: str,
    default: bool,
    input_func: InputFunc,
    output_func: OutputFunc,
) -> bool:
    while True:
        response = input_func(prompt(label, "yes" if default else "no")).strip().lower()
        if not response:
            return default
        if response in {"y", "yes", "true", "1"}:
            return True
        if response in {"n", "no", "false", "0"}:
            return False
        output_func("Please answer yes or no.")


def prompt_optional_float(
    label: str,
    default: Optional[float],
    input_func: InputFunc,
    output_func: OutputFunc,
) -> Optional[float]:
    while True:
        default_text = "" if default is None else format(default, "g")
        response = input_func(prompt(label, default_text)).strip()
        if not response:
            return default
        try:
            value = float(response)
        except ValueError:
            output_func("Please enter a number, or leave it blank to skip.")
            continue
        if value < 0:
            output_func("Please enter zero or a positive number.")
            continue
        return value


def prompt(label: str, default: object) -> str:
    if default in (None, ""):
        return f"{label}: "
    return f"{label} [{default}]: "


def main() -> None:
    run_home_profile_wizard()


if __name__ == "__main__":
    main()
