#!/usr/bin/env python3
"""List battery catalogue products for operator review."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.battery_catalogue import DEFAULT_CATALOGUE_DIR, load_battery_catalogue


def main() -> int:
    catalogue = load_battery_catalogue(DEFAULT_CATALOGUE_DIR)
    print(f"Battery catalogue {catalogue.version} ({catalogue.region})")
    print(f"Products: {len(catalogue.products)}")
    for product in catalogue.products:
        capacities = ", ".join(f"{float(value):g}" for value in product.get("supported_system_capacities_kwh") or [])
        print(
            f"- {product.get('product_id')}: {product.get('manufacturer')} {product.get('model_name')} "
            f"[{capacities} kWh] confidence={product.get('data_confidence')}"
        )
    if catalogue.warnings:
        print("\nWarnings:")
        for warning in catalogue.warnings:
            print(f"- {warning}")
    if catalogue.errors:
        print("\nErrors:")
        for error in catalogue.errors:
            print(f"- {error}")
    return 1 if catalogue.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
