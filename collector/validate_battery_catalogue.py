#!/usr/bin/env python3
"""Validate the source-controlled PowerMonitor battery catalogue."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.battery_catalogue import DEFAULT_CATALOGUE_DIR, load_battery_catalogue


def main() -> int:
    catalogue = load_battery_catalogue(DEFAULT_CATALOGUE_DIR)
    payload = catalogue.summary()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if catalogue.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
