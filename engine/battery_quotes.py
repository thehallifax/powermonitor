"""Battery installer quote persistence and comparison."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from engine.battery_catalogue import BatteryCatalogue, capacity_fit_label, closest_annual_saving, load_battery_catalogue

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUOTES_PATH = PROJECT_ROOT / "config" / "battery_quotes.json"
QUOTE_SCHEMA_VERSION = 1
MAX_ACTIVE_QUOTES = 5

BADGE_BEST_OVERALL = "⭐ Best overall value"
BADGE_LOWEST_UPFRONT = "💰 Lowest upfront cost"
BADGE_MAXIMUM_SAVINGS = "⚡ Maximum annual savings"
BADGE_CLOSEST_FIT = "🎯 Closest capacity fit"
BADGE_REQUIRES_CLARIFICATION = "⚠ Requires clarification"


@dataclass(frozen=True)
class BatteryQuote:
    quote_id: str
    installer_name: str = ""
    installer_notes: str = ""
    quote_date: str = ""
    product_id: str = ""
    configured_modules: Optional[int] = None
    configured_capacity: Optional[float] = None
    inverter_included: bool = False
    backup_gateway_included: bool = False
    installation_scope: str = ""
    monitoring_included: bool = False
    warranty_notes: str = ""
    quoted_price: Optional[float] = None
    rebate: Optional[float] = None
    finance_cost: Optional[float] = None
    quote_status: str = "active"
    homeowner_notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BatteryQuote":
        return cls(
            quote_id=str(data.get("quote_id") or new_quote_id()),
            installer_name=str(data.get("installer_name") or ""),
            installer_notes=str(data.get("installer_notes") or ""),
            quote_date=str(data.get("quote_date") or ""),
            product_id=str(data.get("product_id") or ""),
            configured_modules=_optional_int(data.get("configured_modules")),
            configured_capacity=_optional_float(data.get("configured_capacity")),
            inverter_included=_bool(data.get("inverter_included")),
            backup_gateway_included=_bool(data.get("backup_gateway_included")),
            installation_scope=str(data.get("installation_scope") or ""),
            monitoring_included=_bool(data.get("monitoring_included")),
            warranty_notes=str(data.get("warranty_notes") or ""),
            quoted_price=_optional_float(data.get("quoted_price")),
            rebate=_optional_float(data.get("rebate")),
            finance_cost=_optional_float(data.get("finance_cost")),
            quote_status=str(data.get("quote_status") or "active"),
            homeowner_notes=str(data.get("homeowner_notes") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BatteryQuoteStore:
    path: Path = DEFAULT_QUOTES_PATH

    def load(self) -> list[BatteryQuote]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Battery quotes file must be valid JSON: {self.path}") from exc
        entries = payload.get("quotes") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise ValueError("Battery quotes file must contain a quotes list")
        return [BatteryQuote.from_dict(entry) for entry in entries if isinstance(entry, dict)]

    def save(self, quotes: list[BatteryQuote]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": QUOTE_SCHEMA_VERSION,
            "quotes": [quote.as_dict() for quote in quotes],
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def upsert(self, quote: BatteryQuote) -> list[BatteryQuote]:
        quotes = self.load()
        existing_index = next((index for index, item in enumerate(quotes) if item.quote_id == quote.quote_id), None)
        active_count = sum(1 for item in quotes if item.quote_status != "deleted" and item.quote_id != quote.quote_id)
        if quote.quote_status != "deleted" and active_count >= MAX_ACTIVE_QUOTES and existing_index is None:
            raise ValueError("PowerMonitor supports up to 5 active battery quotes.")
        if existing_index is None:
            quotes.append(quote)
        else:
            quotes[existing_index] = quote
        self.save(quotes)
        return quotes

    def delete(self, quote_id: str) -> list[BatteryQuote]:
        quotes = [quote for quote in self.load() if quote.quote_id != quote_id]
        self.save(quotes)
        return quotes

    def duplicate(self, quote_id: str) -> BatteryQuote:
        quotes = self.load()
        active_count = sum(1 for item in quotes if item.quote_status != "deleted")
        if active_count >= MAX_ACTIVE_QUOTES:
            raise ValueError("PowerMonitor supports up to 5 active battery quotes.")
        original = next((quote for quote in quotes if quote.quote_id == quote_id), None)
        if original is None:
            raise ValueError("Quote not found.")
        duplicate = BatteryQuote.from_dict({**original.as_dict(), "quote_id": new_quote_id(), "installer_name": f"{original.installer_name} copy".strip()})
        quotes.append(duplicate)
        self.save(quotes)
        return duplicate


def compare_battery_quotes(
    quotes: list[BatteryQuote],
    catalogue: Optional[BatteryCatalogue],
    recommended_capacity_kwh: float,
    investment: dict[str, Any],
) -> dict[str, Any]:
    catalogue = catalogue or load_battery_catalogue()
    products = {str(product.get("product_id")): product for product in catalogue.products}
    active_quote_entries = [quote for quote in quotes if quote.quote_status != "deleted"]
    active_quotes = active_quote_entries[:MAX_ACTIVE_QUOTES]
    comparisons = [
        compare_battery_quote(quote, products.get(quote.product_id), recommended_capacity_kwh, investment)
        for quote in active_quotes
    ]
    assign_quote_badges(comparisons)
    summary = quote_comparison_summary(comparisons)
    return {
        "recommended_capacity_kwh": recommended_capacity_kwh,
        "active_quote_count": len(active_quotes),
        "max_active_quotes": MAX_ACTIVE_QUOTES,
        "catalogue_products": quote_catalogue_options(catalogue),
        "quotes": [quote.as_dict() for quote in active_quotes],
        "comparisons": comparisons,
        "summary": summary,
        "warnings": [] if len(active_quote_entries) <= MAX_ACTIVE_QUOTES else ["Only the first 5 active quotes are compared."],
    }


def compare_battery_quote(
    quote: BatteryQuote,
    product: Optional[dict[str, Any]],
    recommended_capacity_kwh: float,
    investment: dict[str, Any],
) -> dict[str, Any]:
    capacity = quote_capacity(quote, product)
    difference = capacity - recommended_capacity_kwh if recommended_capacity_kwh else 0.0
    difference_pct = difference / recommended_capacity_kwh * 100 if recommended_capacity_kwh else 0.0
    annual_saving = closest_annual_saving(capacity, investment) or _optional_float(investment.get("annual_saving")) or 0.0
    net_price = quote_net_price(quote)
    payback = net_price / annual_saving if net_price is not None and annual_saving > 0 else None
    assumptions = investment.get("assumptions") or {}
    ten_year_savings = projected_savings(annual_saving, 10, assumptions)
    fifteen_year_savings = projected_savings(annual_saving, 15, assumptions)
    ten_year_net = ten_year_savings - net_price if net_price is not None else None
    fifteen_year_net = fifteen_year_savings - net_price if net_price is not None else None
    missing = quote_missing_information(quote, product)
    capacity_fit = capacity_fit_label(difference, difference_pct) if recommended_capacity_kwh else "Poor capacity fit"
    investment_score = score_investment(payback, fifteen_year_net)
    recommendation_score = score_recommendation(capacity_fit, payback, fifteen_year_net, net_price)
    return {
        "quote_id": quote.quote_id,
        "installer_name": quote.installer_name or "Unnamed installer",
        "installer_notes": quote.installer_notes,
        "quote_date": quote.quote_date,
        "product_id": quote.product_id,
        "product_name": product_name(product),
        "configured_modules": quote.configured_modules,
        "configured_capacity": round(capacity, 2),
        "usable_capacity": round(capacity, 2),
        "quoted_price": quote.quoted_price,
        "rebate": quote.rebate,
        "finance_cost": quote.finance_cost,
        "net_price": net_price,
        "estimated_annual_bill_reduction": annual_saving,
        "simple_payback_years": payback,
        "ten_year_net_benefit": ten_year_net,
        "fifteen_year_net_benefit": fifteen_year_net,
        "capacity_difference_kwh": round(difference, 2),
        "capacity_difference_pct": round(difference_pct, 1),
        "capacity_fit": capacity_fit,
        "investment_score": investment_score,
        "recommendation_score": recommendation_score,
        "primary_badge": "",
        "badge_explanation": "",
        "missing_information": missing,
        "inverter_included": quote.inverter_included,
        "backup_gateway_included": quote.backup_gateway_included,
        "installation_scope": quote.installation_scope,
        "monitoring_included": quote.monitoring_included,
        "warranty_notes": quote.warranty_notes,
        "homeowner_notes": quote.homeowner_notes,
        "compatibility_notes": compatibility_notes(product),
    }


def quote_capacity(quote: BatteryQuote, product: Optional[dict[str, Any]]) -> float:
    if product and product.get("modular") and quote.configured_modules and product.get("module_capacity_kwh"):
        return round(float(product["module_capacity_kwh"]) * int(quote.configured_modules), 2)
    if quote.configured_capacity:
        return float(quote.configured_capacity)
    capacities = product.get("supported_system_capacities_kwh") if product else None
    if capacities:
        return float(capacities[0])
    return float((product or {}).get("usable_capacity_kwh") or 0.0)


def quote_net_price(quote: BatteryQuote) -> Optional[float]:
    if quote.quoted_price is None:
        return None
    return max(float(quote.quoted_price) + float(quote.finance_cost or 0.0) - float(quote.rebate or 0.0), 0.0)


def quote_missing_information(quote: BatteryQuote, product: Optional[dict[str, Any]]) -> list[str]:
    missing = []
    if product is None:
        missing.append("Product is not in the current battery catalogue.")
    if not quote.inverter_included:
        missing.append("Confirm whether a required inverter is included.")
    if product and product.get("backup_capable") and not quote.backup_gateway_included:
        missing.append("Confirm whether the backup gateway or controller is included.")
    if product and product.get("backup_capable") is None:
        missing.append("Backup capability is unclear.")
    if not quote.warranty_notes:
        missing.append("Warranty conditions are missing.")
    if quote.rebate is None:
        missing.append("Rebate treatment is unclear.")
    if quote.quoted_price is None:
        missing.append("Quoted price is missing.")
    return missing


def assign_quote_badges(comparisons: list[dict[str, Any]]) -> None:
    if not comparisons:
        return
    priced = [item for item in comparisons if item.get("net_price") is not None]
    best = max(priced or comparisons, key=lambda item: item.get("recommendation_score") or 0)
    lowest = min(priced, key=lambda item: item["net_price"]) if priced else None
    maximum = max(comparisons, key=lambda item: item.get("estimated_annual_bill_reduction") or 0)
    closest = min(comparisons, key=lambda item: abs(float(item.get("capacity_difference_kwh") or 0.0)))
    for item in comparisons:
        if item.get("net_price") is None or item.get("product_id") == "":
            badge = BADGE_REQUIRES_CLARIFICATION
            reason = "This quote needs price or product details before it can be compared confidently."
        elif item is best:
            badge = BADGE_BEST_OVERALL
            reason = "This quote has the strongest balance of capacity fit, payback, net benefit and price."
        elif lowest and item is lowest:
            badge = BADGE_LOWEST_UPFRONT
            reason = "This quote has the lowest quoted net upfront cost after entered rebates and finance costs."
        elif item is maximum:
            badge = BADGE_MAXIMUM_SAVINGS
            reason = "This quote is estimated to deliver the highest annual bill reduction using existing battery modelling."
        elif item is closest:
            badge = BADGE_CLOSEST_FIT
            reason = "This quote is closest to PowerMonitor's recommended usable battery capacity."
        else:
            badge = BADGE_REQUIRES_CLARIFICATION
            reason = "Review inclusions, warranty, rebate and compatibility details before relying on this quote."
        item["primary_badge"] = badge
        item["badge_explanation"] = reason


def quote_comparison_summary(comparisons: list[dict[str, Any]]) -> str:
    if not comparisons:
        return "Add up to 5 installer quotes to compare them against the existing battery recommendation."
    best = next((item for item in comparisons if item.get("primary_badge") == BADGE_BEST_OVERALL), comparisons[0])
    return (
        f"{best['installer_name']} offers the best overall value. "
        "It balances the modelled capacity fit, estimated savings and quoted net price using PowerMonitor's existing battery modelling."
    )


def product_name(product: Optional[dict[str, Any]]) -> str:
    if not product:
        return "Unknown product"
    return f"{product.get('manufacturer', '')} {product.get('model_name', '')}".strip()


def compatibility_notes(product: Optional[dict[str, Any]]) -> list[str]:
    if not product:
        return ["Product compatibility could not be checked against the catalogue."]
    notes = []
    if product.get("gateway_or_controller_required"):
        notes.append(str(product["gateway_or_controller_required"]))
    if product.get("installation_notes"):
        notes.append(str(product["installation_notes"]))
    return notes or ["Confirm final compatibility and installation design with the installer."]


def quote_catalogue_options(catalogue: BatteryCatalogue) -> list[dict[str, Any]]:
    options = []
    for product in catalogue.products:
        product_id = str(product.get("product_id") or "")
        if not product_id:
            continue
        manufacturer = str(product.get("manufacturer") or "")
        model = str(product.get("model_name") or "")
        capacities = product.get("supported_system_capacities_kwh") or []
        options.append(
            {
                "product_id": product_id,
                "label": f"{manufacturer} {model}".strip() or product_id,
                "manufacturer": manufacturer,
                "model_name": model,
                "modular": bool(product.get("modular")),
                "module_capacity_kwh": _optional_float(product.get("module_capacity_kwh")),
                "minimum_modules": _optional_int(product.get("minimum_modules")),
                "maximum_modules": _optional_int(product.get("maximum_modules")),
                "usable_capacity_kwh": _optional_float(product.get("usable_capacity_kwh")),
                "supported_system_capacities_kwh": [float(value) for value in capacities],
            }
        )
    return sorted(options, key=lambda item: item["label"].lower())


def score_investment(payback: Optional[float], fifteen_year_net: Optional[float]) -> int:
    score = 50
    if payback is not None:
        score += max(0, min(30, int((15 - payback) * 3)))
    if fifteen_year_net is not None:
        score += 20 if fifteen_year_net > 0 else -15
    return max(0, min(100, score))


def score_recommendation(capacity_fit: str, payback: Optional[float], fifteen_year_net: Optional[float], net_price: Optional[float]) -> int:
    score = {
        "Excellent fit": 45,
        "Close fit": 35,
        "Smaller than recommended": 25,
        "Larger than recommended": 25,
        "Poor capacity fit": 10,
    }.get(capacity_fit, 10)
    score += score_investment(payback, fifteen_year_net) // 2
    if net_price is None:
        score -= 20
    return max(0, min(100, score))


def projected_savings(annual_saving: float, years: int, assumptions: dict[str, Any]) -> float:
    total = 0.0
    degradation = 1 - max(float(assumptions.get("annual_degradation_pct") or 0.0), 0.0) / 100
    inflation = 1 + float(assumptions.get("electricity_inflation_pct") or 0.0) / 100
    discount = 1 + max(float(assumptions.get("discount_rate_pct") or 0.0), 0.0) / 100
    for year in range(years):
        total += annual_saving * (degradation ** year) * (inflation ** year) / (discount ** year)
    return total


def normalize_quote_payload(data: dict[str, Any], catalogue: Optional[BatteryCatalogue] = None, existing_quote_id: Optional[str] = None) -> BatteryQuote:
    catalogue = catalogue or load_battery_catalogue()
    products = {str(product.get("product_id")): product for product in catalogue.products}
    product_id = str(data.get("product_id") or "")
    product = products.get(product_id)
    quote_id = str(data.get("quote_id") or existing_quote_id or new_quote_id())
    modules = _optional_int(data.get("configured_modules"))
    capacity = _optional_float(data.get("configured_capacity"))
    if product and product.get("modular") and modules and product.get("module_capacity_kwh"):
        capacity = round(float(product["module_capacity_kwh"]) * modules, 2)
    elif product and capacity is None:
        capacities = product.get("supported_system_capacities_kwh") or []
        capacity = float(capacities[0]) if capacities else _optional_float(product.get("usable_capacity_kwh"))
    return BatteryQuote.from_dict({**data, "quote_id": quote_id, "configured_modules": modules, "configured_capacity": capacity})


def new_quote_id() -> str:
    return "quote_" + uuid.uuid4().hex[:12]


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "included"}
    return bool(value)


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
