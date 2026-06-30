import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "power.sqlite"
DEFAULT_HOUSEHOLD_ID = "default_household"

RATES = {
    "super_off_peak": 0.086151,
    "overnight": 0.193841,
    "off_peak": 0.236916,
    "peak": 0.538446,
}


def bucket_for_time(dt):
    hour = dt.hour

    if hour >= 23 or hour < 6:
        return "overnight"

    if 6 <= hour < 9:
        return "off_peak"

    if 9 <= hour < 15:
        return "super_off_peak"

    if 15 <= hour < 21:
        return "peak"

    return "off_peak"


def watts_to_kwh_15min(value):
    if value is None:
        return 0.0

    return float(value) * 0.25 / 1000.0


def main():
    if len(sys.argv) != 2:
        print("Usage:")
        print("python3 collector/import_solaredge.py file.json")
        sys.exit(1)

    json_file = Path(sys.argv[1])

    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS readings (
        household_id TEXT DEFAULT 'default_household',
        measurement_time TEXT PRIMARY KEY,
        tariff_bucket TEXT,

        production_w REAL,
        consumption_w REAL,

        solar_to_home_w REAL,
        solar_to_grid_w REAL,
        grid_to_home_w REAL,

        production_kwh REAL,
        consumption_kwh REAL,

        solar_to_home_kwh REAL,
        solar_to_grid_kwh REAL,
        grid_to_home_kwh REAL,

        import_cost REAL
    )
    """)
    present = {row[1] for row in cur.execute("PRAGMA table_info(readings)")}
    if "household_id" not in present:
        cur.execute("ALTER TABLE readings ADD COLUMN household_id TEXT")
    cur.execute(
        "UPDATE readings SET household_id = ? WHERE household_id IS NULL OR household_id = ''",
        (DEFAULT_HOUSEHOLD_ID,),
    )

    received = 0
    imported = 0
    duplicates_ignored = 0
    imported_times = []

    for m in data.get("measurements", []):
        received += 1

        ts = m.get("measurementTime")

        if not ts:
            continue

        dt = datetime.fromisoformat(ts)

        bucket = bucket_for_time(dt)

        production_w = m.get("production")
        consumption_w = m.get("consumption")

        production_dist = m.get("productionDistribution") or {}
        consumption_dist = m.get("consumptionDistribution") or {}

        solar_to_home_w = production_dist.get("productionToHome")
        solar_to_grid_w = production_dist.get("productionToGrid")

        grid_to_home_w = consumption_dist.get("consumptionFromGrid")

        production_kwh = watts_to_kwh_15min(production_w)
        consumption_kwh = watts_to_kwh_15min(consumption_w)

        solar_to_home_kwh = watts_to_kwh_15min(solar_to_home_w)
        solar_to_grid_kwh = watts_to_kwh_15min(solar_to_grid_w)

        grid_to_home_kwh = watts_to_kwh_15min(grid_to_home_w)

        import_cost = grid_to_home_kwh * RATES[bucket]

        cur.execute(
            "SELECT 1 FROM readings WHERE household_id = ? AND measurement_time = ?",
            (DEFAULT_HOUSEHOLD_ID, ts),
        )
        if cur.fetchone():
            duplicates_ignored += 1
            continue

        cur.execute("""
        INSERT INTO readings (
            household_id,
            measurement_time,
            tariff_bucket,

            production_w,
            consumption_w,

            solar_to_home_w,
            solar_to_grid_w,
            grid_to_home_w,

            production_kwh,
            consumption_kwh,

            solar_to_home_kwh,
            solar_to_grid_kwh,
            grid_to_home_kwh,

            import_cost
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            DEFAULT_HOUSEHOLD_ID,
            ts,
            bucket,

            production_w,
            consumption_w,

            solar_to_home_w,
            solar_to_grid_w,
            grid_to_home_w,

            production_kwh,
            consumption_kwh,

            solar_to_home_kwh,
            solar_to_grid_kwh,
            grid_to_home_kwh,

            import_cost
        ))

        imported += 1
        imported_times.append(dt)

    conn.commit()

    if imported_times:
        start_time = min(imported_times).isoformat()
        end_time = max(imported_times).isoformat()
    else:
        start_time = "none"
        end_time = "none"
    observations_promoted = 0
    print(
        "SolarEdge import complete: "
        f"data received {received}; "
        f"time range imported {start_time} to {end_time}; "
        f"records imported {imported}; "
        f"duplicates ignored {duplicates_ignored}; "
        f"observations promoted {observations_promoted}"
    )
    print(f"Imported {imported} readings")

    conn.close()


if __name__ == "__main__":
    main()
