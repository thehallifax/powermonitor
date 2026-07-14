"""Battery product catalogue loading, validation, and capacity matching."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOGUE_DIR = PROJECT_ROOT / "data" / "catalogues" / "batteries"
CATALOGUE_DISCLAIMER = (
    "Product matching is based on published specifications and your PowerMonitor household model. "
    "Final compatibility, installation design, warranty conditions and pricing must be confirmed by a qualified installer."
)

VALID_REGIONS = {"AU", "US", "EU", "UK", "NZ", "GLOBAL"}
VALID_CURRENCIES = {"AUD", "USD", "EUR", "GBP", "NZD", None, ""}
VALID_LIFECYCLE = {"active", "current", "legacy", "discontinued", "unknown"}
VALID_STATUS = {"active", "available", "current", "legacy", "discontinued", "unknown"}
VALID_CONFIDENCE = {"Low", "Medium", "High", "Very High"}


@dataclass(frozen=True)
class BatteryCatalogue:
    version: str
    region: str
    checked_at: str
    products: list[dict[str, Any]]
    warnings: list[str]
    errors: list[str]

    @property
    def valid(self) -> bool:
        return not self.errors

    def summary(self) -> dict[str, Any]:
        active_au = [
            product for product in self.products
            if str(product.get("region") or "").upper() == "AU"
            and str(product.get("lifecycle_status") or product.get("status") or "").lower() in {"active", "current", "available"}
        ]
        missing_cost = [
            product["product_id"] for product in self.products
            if product.get("estimated_installed_cost_low") is None or product.get("estimated_installed_cost_high") is None
        ]
        uncertain_compatibility = [
            product["product_id"] for product in self.products
            if not product.get("compatible_inverter_families")
            or product.get("single_phase_support") is None
            or product.get("three_phase_support") is None
        ]
        return {
            "version": self.version,
            "region": self.region,
            "product_count": len(self.products),
            "australian_active_product_count": len(active_au),
            "validation_status": "valid" if self.valid else "invalid",
            "checked_at": self.checked_at,
            "last_source_review_dates": sorted(
                {str(product.get("source_checked_at")) for product in self.products if product.get("source_checked_at")}
            ),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "products_missing_cost_data": missing_cost,
            "products_with_uncertain_compatibility_data": uncertain_compatibility,
        }


def load_battery_catalogue(catalogue_dir: Path = DEFAULT_CATALOGUE_DIR) -> BatteryCatalogue:
    products: list[dict[str, Any]] = []
    versions = []
    regions = []
    checked_dates = []
    warnings: list[str] = []
    errors: list[str] = []
    if not catalogue_dir.exists():
        return BatteryCatalogue("", "AU", "", [], [f"Catalogue directory not found: {catalogue_dir}"], [])
    for path in sorted(catalogue_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: cannot read catalogue JSON ({exc})")
            continue
        entries = payload.get("products") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            errors.append(f"{path.name}: catalogue must contain a products list")
            continue
        versions.append(str(payload.get("catalogue_version") or payload.get("version") or path.stem))
        regions.append(str(payload.get("region") or "AU").upper())
        checked_dates.append(str(payload.get("checked_at") or ""))
        for product in entries:
            if isinstance(product, dict):
                products.append(product)
            else:
                errors.append(f"{path.name}: product entry is not an object")
    validation = validate_battery_catalogue(products)
    warnings.extend(validation["warnings"])
    errors.extend(validation["errors"])
    return BatteryCatalogue(
        version=", ".join(sorted(set(filter(None, versions)))),
        region=", ".join(sorted(set(filter(None, regions)))) or "AU",
        checked_at=max(filter(None, checked_dates), default=""),
        products=products,
        warnings=warnings,
        errors=errors,
    )


def validate_battery_catalogue(products: list[dict[str, Any]]) -> dict[str, list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_fingerprints: set[tuple[str, str, str]] = set()
    for index, product in enumerate(products):
        label = str(product.get("product_id") or f"product[{index}]")
        product_id = str(product.get("product_id") or "").strip()
        if not product_id:
            errors.append(f"{label}: product_id is required")
        elif product_id in seen_ids:
            errors.append(f"{label}: duplicate product_id")
        seen_ids.add(product_id)
        fingerprint = (
            str(product.get("manufacturer") or "").strip().lower(),
            str(product.get("product_family") or "").strip().lower(),
            str(product.get("model_name") or "").strip().lower(),
        )
        if all(fingerprint):
            if fingerprint in seen_fingerprints:
                warnings.append(f"{label}: duplicate manufacturer/family/model")
            seen_fingerprints.add(fingerprint)
        usable = _optional_float(product.get("usable_capacity_kwh"))
        if usable is None or usable <= 0:
            errors.append(f"{label}: usable_capacity_kwh must be positive")
        _validate_required_text(product, "manufacturer", label, errors)
        _validate_required_text(product, "model_name", label, errors)
        _validate_required_text(product, "source", label, errors)
        _validate_required_text(product, "source_url", label, errors)
        _validate_iso_date(product.get("source_checked_at"), f"{label}: source_checked_at", errors)
        confidence = product.get("data_confidence")
        if confidence not in VALID_CONFIDENCE:
            errors.append(f"{label}: data_confidence must be one of {', '.join(sorted(VALID_CONFIDENCE))}")
        region = str(product.get("region") or "").upper()
        if region not in VALID_REGIONS:
            errors.append(f"{label}: unsupported region {region or 'blank'}")
        lifecycle = str(product.get("lifecycle_status") or "").lower()
        if lifecycle not in VALID_LIFECYCLE:
            errors.append(f"{label}: lifecycle_status is invalid")
        status = str(product.get("status") or "").lower()
        if status not in VALID_STATUS:
            errors.append(f"{label}: status is invalid")
        currency = product.get("cost_currency")
        if currency not in VALID_CURRENCIES:
            errors.append(f"{label}: cost_currency is invalid")
        if product.get("cost_checked_at"):
            _validate_iso_date(product.get("cost_checked_at"), f"{label}: cost_checked_at", errors)
        low = _optional_float(product.get("estimated_installed_cost_low"))
        high = _optional_float(product.get("estimated_installed_cost_high"))
        if low is not None and high is not None and low > high:
            errors.append(f"{label}: installed-cost low must be <= high")
        for field in ("round_trip_efficiency_pct", "minimum_state_of_charge_pct"):
            value = _optional_float(product.get(field))
            if value is not None and not 0 <= value <= 100:
                errors.append(f"{label}: {field} must be between 0 and 100")
        modular = bool(product.get("modular"))
        supported = [_optional_float(item) for item in product.get("supported_system_capacities_kwh") or []]
        supported = [item for item in supported if item is not None]
        if not supported:
            errors.append(f"{label}: supported_system_capacities_kwh must not be empty")
        if any(item <= 0 for item in supported):
            errors.append(f"{label}: supported capacities must be positive")
        if modular:
            module_capacity = _optional_float(product.get("module_capacity_kwh"))
            min_modules = _optional_int(product.get("minimum_modules"))
            max_modules = _optional_int(product.get("maximum_modules"))
            if module_capacity is not None and module_capacity <= 0:
                errors.append(f"{label}: module_capacity_kwh must be positive")
            if min_modules is not None and min_modules <= 0:
                errors.append(f"{label}: minimum_modules must be positive")
            if max_modules is not None and min_modules is not None and max_modules < min_modules:
                errors.append(f"{label}: maximum_modules must be >= minimum_modules")
            if module_capacity and min_modules and max_modules and supported:
                expected = {round(module_capacity * modules, 2) for modules in range(min_modules, max_modules + 1)}
                rounded_supported = {round(value, 2) for value in supported}
                if not rounded_supported.issubset(expected):
                    warnings.append(f"{label}: supported capacities are not a simple module multiple; verify source rounding")
        else:
            if usable is not None and supported and min(abs(value - usable) for value in supported) > 0.15:
                warnings.append(f"{label}: fixed supported capacity differs from usable_capacity_kwh")
        checksum = str(product.get("checksum") or "")
        if not checksum.startswith("sha256:") or len(checksum) != 71:
            errors.append(f"{label}: checksum must be sha256:<64 hex>")
        elif checksum != product_checksum(product):
            errors.append(f"{label}: checksum does not match product contents")
    return {"warnings": warnings, "errors": errors}


def product_checksum(product: dict[str, Any]) -> str:
    canonical = {key: value for key, value in product.items() if key != "checksum"}
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def match_battery_products(
    recommended_capacity_kwh: float,
    catalogue: Optional[BatteryCatalogue] = None,
    *,
    household_context: Optional[dict[str, Any]] = None,
    investment: Optional[dict[str, Any]] = None,
    max_matches: int = 5,
) -> dict[str, Any]:
    catalogue = catalogue or load_battery_catalogue()
    context = household_context or {}
    if not recommended_capacity_kwh or recommended_capacity_kwh <= 0:
        return {
            "recommended_capacity_kwh": recommended_capacity_kwh,
            "household_context": safe_household_context(context),
            "matches": [],
            "warnings": ["Battery product matching needs a positive recommended usable capacity."],
            "disclaimer": CATALOGUE_DISCLAIMER,
        }
    matches = [
        product_match(product, recommended_capacity_kwh, context, investment or {})
        for product in catalogue.products
        if str(product.get("region") or "").upper() in {"AU", "GLOBAL"}
        and str(product.get("lifecycle_status") or product.get("status") or "").lower() not in {"discontinued"}
    ]
    matches.sort(key=lambda item: item["_rank"])
    safe_matches = [{key: value for key, value in match.items() if key != "_rank"} for match in matches[:max_matches]]
    return {
        "recommended_capacity_kwh": recommended_capacity_kwh,
        "household_context": safe_household_context(context),
        "matches": safe_matches,
        "warnings": list(catalogue.warnings) + list(catalogue.errors),
        "disclaimer": CATALOGUE_DISCLAIMER,
    }


def product_match(
    product: dict[str, Any],
    recommended_capacity: float,
    household_context: dict[str, Any],
    investment: dict[str, Any],
) -> dict[str, Any]:
    configured_capacity, modules = configured_capacity_for(product, recommended_capacity)
    difference = configured_capacity - recommended_capacity
    pct = difference / recommended_capacity * 100 if recommended_capacity else 0.0
    fit = capacity_fit_label(difference, pct)
    compatibility_status, reasons, compatibility_warnings = compatibility_for(product, household_context)
    missing = missing_information_warnings(product)
    cost_range = cost_range_for(product)
    investment_estimate = product_investment_estimate(configured_capacity, cost_range, investment)
    confidence_rank = {"Very High": 0, "High": 1, "Medium": 2, "Low": 3}.get(str(product.get("data_confidence")), 4)
    lifecycle_penalty = 0 if str(product.get("lifecycle_status") or "").lower() in {"active", "current"} else 3
    cost_penalty = 0 if cost_range else 1
    compatibility_penalty = {
        "Compatible": 0,
        "Likely compatible": 0.5,
        "Requires installer confirmation": 1,
        "Additional inverter/controller required": 2,
        "Unknown": 2.5,
        "Not compatible": 12,
    }.get(compatibility_status, 3)
    rank = abs(pct) + (1.5 if difference < 0 else 0.6 if difference > 0 else 0.0) + confidence_rank + lifecycle_penalty + cost_penalty + compatibility_penalty
    return {
        "_rank": rank,
        "product_id": product.get("product_id"),
        "manufacturer": product.get("manufacturer"),
        "product_family": product.get("product_family"),
        "model_name": product.get("model_name"),
        "configured_capacity_kwh": round(configured_capacity, 2),
        "capacity_difference_kwh": round(difference, 2),
        "capacity_difference_pct": round(pct, 1),
        "relative_to_recommendation": "above" if difference > 0 else "below" if difference < 0 else "matched",
        "module_configuration": module_configuration(product, modules, configured_capacity),
        "capacity_fit": fit,
        "technical_fit_status": compatibility_status,
        "compatibility_status": compatibility_status,
        "compatibility_reasons": reasons,
        "warnings": compatibility_warnings + missing,
        "missing_information_warnings": missing,
        "backup_capable": product.get("backup_capable"),
        "continuous_charge_power_kw": product.get("continuous_charge_power_kw"),
        "continuous_discharge_power_kw": product.get("continuous_discharge_power_kw"),
        "peak_discharge_power_kw": product.get("peak_discharge_power_kw"),
        "cost_range": cost_range,
        "estimated_annual_bill_reduction": investment_estimate.get("annual_saving"),
        "estimated_payback_range": investment_estimate.get("payback_range"),
        "ten_year_net_benefit_range": investment_estimate.get("ten_year_net_benefit_range"),
        "fifteen_year_net_benefit_range": investment_estimate.get("fifteen_year_net_benefit_range"),
        "generic_investment_comparison": investment_estimate.get("generic_investment_comparison"),
        "data_confidence": product.get("data_confidence"),
        "source_checked_at": product.get("source_checked_at"),
        "source": product.get("source"),
        "source_url": product.get("source_url"),
        "quote_comparison_seed": {
            "product_id": product.get("product_id"),
            "configured_modules": modules,
            "configured_usable_capacity_kwh": round(configured_capacity, 2),
            "inverter_controller_inclusions": product.get("gateway_or_controller_required"),
            "backup_gateway": product.get("gateway_or_controller_required") if product.get("backup_capable") else None,
            "installation_scope": product.get("installation_notes"),
            "warranty": warranty_summary(product),
            "quoted_price": None,
        },
    }


def configured_capacity_for(product: dict[str, Any], recommended_capacity: float) -> tuple[float, Optional[int]]:
    supported = sorted(float(value) for value in product.get("supported_system_capacities_kwh") or [] if _optional_float(value))
    if not supported:
        return float(product.get("usable_capacity_kwh") or 0.0), None
    capacity = min(supported, key=lambda value: (abs(value - recommended_capacity), value < recommended_capacity, value))
    modules = None
    if product.get("modular") and product.get("module_capacity_kwh"):
        modules = max(1, round(capacity / float(product["module_capacity_kwh"])))
    return capacity, modules


def capacity_fit_label(difference_kwh: float, difference_pct: float) -> str:
    absolute_pct = abs(difference_pct)
    absolute_kwh = abs(difference_kwh)
    if absolute_pct <= 5 or absolute_kwh <= 0.75:
        return "Excellent fit"
    if absolute_pct <= 15 or absolute_kwh <= 2.0:
        return "Close fit"
    if absolute_pct > 40:
        return "Poor capacity fit"
    return "Smaller than recommended" if difference_kwh < 0 else "Larger than recommended"


def compatibility_for(product: dict[str, Any], household_context: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    reasons: list[str] = []
    warnings: list[str] = []
    inverter_brand = str(household_context.get("inverter_brand") or "").strip().lower()
    inverter_family = str(household_context.get("inverter_family") or household_context.get("inverter_model") or "").strip().lower()
    phase = str(household_context.get("phase_configuration") or "").strip().lower()
    backup_required = bool(household_context.get("backup_required"))
    families = [str(item).lower() for item in product.get("compatible_inverter_families") or []]
    integrated_inverter = bool(product.get("integrated_inverter"))
    if inverter_brand == "solaredge" and families and any("solaredge" in item for item in families):
        status = "Likely compatible"
        reasons.append("Catalogue lists SolarEdge-compatible inverter families.")
    elif inverter_family and families and any(family and family in inverter_family for family in families):
        status = "Likely compatible"
        reasons.append("Household inverter family appears in the catalogue compatibility list.")
    elif integrated_inverter:
        status = "Requires installer confirmation"
        reasons.append("Product includes an inverter or power conversion stage, so integration design must be confirmed.")
    elif families:
        status = "Additional inverter/controller required"
        reasons.append("Catalogue compatibility list does not match the known household inverter context.")
    else:
        status = "Unknown"
        warnings.append("Published inverter compatibility is not complete in the catalogue.")
    if phase == "single_phase" and product.get("single_phase_support") is False:
        status = "Requires installer confirmation" if status != "Not compatible" else status
        warnings.append("Single-phase support is not published for this product.")
    if phase == "three_phase" and product.get("three_phase_support") is False:
        status = "Requires installer confirmation" if status != "Not compatible" else status
        warnings.append("Three-phase support is not published for this product.")
    if not phase:
        warnings.append("Household phase configuration is unknown.")
    if backup_required and product.get("backup_capable") is False:
        status = "Requires installer confirmation"
        warnings.append("Backup requirement is configured but backup capability is not published as supported.")
    if product.get("gateway_or_controller_required"):
        warnings.append(str(product["gateway_or_controller_required"]))
    if not reasons:
        reasons.append("Installer confirmation is required before treating this as compatible.")
    return status, reasons, warnings


def missing_information_warnings(product: dict[str, Any]) -> list[str]:
    warnings = []
    if product.get("estimated_installed_cost_low") is None or product.get("estimated_installed_cost_high") is None:
        warnings.append("Indicative installed cost is not published in the catalogue.")
    if product.get("continuous_discharge_power_kw") is None:
        warnings.append("Continuous discharge power is not published in the catalogue.")
    if product.get("compatible_inverter_families") in (None, []):
        warnings.append("Compatible inverter families are not fully published.")
    return warnings


def cost_range_for(product: dict[str, Any]) -> Optional[dict[str, Any]]:
    low = _optional_float(product.get("estimated_installed_cost_low"))
    high = _optional_float(product.get("estimated_installed_cost_high"))
    if low is None or high is None:
        return None
    return {
        "low": low,
        "high": high,
        "currency": product.get("cost_currency") or "AUD",
        "region": product.get("cost_region") or product.get("region") or "AU",
        "checked_at": product.get("cost_checked_at"),
        "source": "Indicative catalogue range; quote required.",
    }


def product_investment_estimate(configured_capacity: float, cost_range: Optional[dict[str, Any]], investment: dict[str, Any]) -> dict[str, Any]:
    annual_saving = closest_annual_saving(configured_capacity, investment)
    if annual_saving is None:
        return {"annual_saving": None}
    result: dict[str, Any] = {"annual_saving": annual_saving}
    if not cost_range:
        return result
    rebate = float((investment.get("assumptions") or {}).get("government_rebate") or 0.0)
    low_cost = max(float(cost_range["low"]) - rebate, 0.0)
    high_cost = max(float(cost_range["high"]) - rebate, 0.0)
    result["payback_range"] = {
        "low_years": round(low_cost / annual_saving, 1) if annual_saving > 0 else None,
        "high_years": round(high_cost / annual_saving, 1) if annual_saving > 0 else None,
        "rebate_assumption": rebate,
    }
    assumptions = investment.get("assumptions") or {}
    ten_year_savings = _projected_savings_from_investment(annual_saving, 10, assumptions)
    fifteen_year_savings = _projected_savings_from_investment(annual_saving, 15, assumptions)
    result["ten_year_net_benefit_range"] = {"low": round(ten_year_savings - high_cost, 2), "high": round(ten_year_savings - low_cost, 2)}
    result["fifteen_year_net_benefit_range"] = {"low": round(fifteen_year_savings - high_cost, 2), "high": round(fifteen_year_savings - low_cost, 2)}
    generic_net = _optional_float(investment.get("net_cost"))
    if generic_net is not None:
        result["generic_investment_comparison"] = {
            "generic_net_cost": generic_net,
            "product_net_cost_low": low_cost,
            "product_net_cost_high": high_cost,
        }
    return result


def closest_annual_saving(configured_capacity: float, investment: dict[str, Any]) -> Optional[float]:
    options = investment.get("options") or []
    if not options:
        return _optional_float(investment.get("annual_saving"))
    closest = min(options, key=lambda item: abs(float(item.get("capacity_kwh") or 0.0) - configured_capacity))
    return _optional_float(closest.get("annual_saving"))


def _projected_savings_from_investment(annual_saving: float, years: int, assumptions: dict[str, Any]) -> float:
    total = 0.0
    degradation = 1 - max(float(assumptions.get("annual_degradation_pct") or 0.0), 0.0) / 100
    inflation = 1 + float(assumptions.get("electricity_inflation_pct") or 0.0) / 100
    discount = 1 + max(float(assumptions.get("discount_rate_pct") or 0.0), 0.0) / 100
    for year in range(years):
        total += annual_saving * (degradation ** year) * (inflation ** year) / (discount ** year)
    return total


def module_configuration(product: dict[str, Any], modules: Optional[int], configured_capacity: float) -> dict[str, Any]:
    return {
        "modular": bool(product.get("modular")),
        "modules": modules,
        "module_capacity_kwh": product.get("module_capacity_kwh"),
        "configured_usable_capacity_kwh": round(configured_capacity, 2),
    }


def warranty_summary(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "years": product.get("warranty_years"),
        "cycles": product.get("warranty_cycles"),
        "throughput_mwh": product.get("warranty_throughput_mwh"),
    }


def safe_household_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "region": context.get("region") or "AU",
        "inverter_brand_known": bool(context.get("inverter_brand")),
        "inverter_family_known": bool(context.get("inverter_family") or context.get("inverter_model")),
        "phase_configuration_known": bool(context.get("phase_configuration")),
        "backup_required": bool(context.get("backup_required")),
    }


def _validate_required_text(product: dict[str, Any], field: str, label: str, errors: list[str]) -> None:
    if not str(product.get(field) or "").strip():
        errors.append(f"{label}: {field} is required")


def _validate_iso_date(value: Any, label: str, errors: list[str]) -> None:
    try:
        date.fromisoformat(str(value))
    except (TypeError, ValueError):
        errors.append(f"{label} must be an ISO date")


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
