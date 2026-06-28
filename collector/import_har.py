import json
import sqlite3
import sys
from datetime import datetime, date
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

SUPPLY_CHARGE_PER_DAY = 1.292269


def bucket_for_time(dt: datetime) -> str:
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


def watts_to_kwh_15min(watts):
    if watts is None:
        return 0.0
    return float(watts) * 0.25 / 1000


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 collector/import_har.py /path/to/monitoring.solaredge.com.har")
        sys.exit(1)

    har_path = Path(sys.argv[1])

    with har_path.open("r", encoding="utf-8", errors="replace") as f:
        har = json.load(f)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS readings (
        household_id TEXT DEFAULT 'default_household',
        measurement_time TEXT PRIMARY KEY,
        tariff_bucket TEXT NOT NULL,

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

    inserted = 0

    for entry in har["log"]["entries"]:
        url = entry.get("request", {}).get("url", "")

        if "/services/dashboard/power/sites/" not in url:
            continue

        text = entry.get("response", {}).get("content", {}).get("text")
        if not text:
            continue

        data = json.loads(text)

        for m in data.get("measurements", []):
            ts = m.get("measurementTime")
            if not ts:
                continue

            dt = datetime.fromisoformat(ts)
            bucket = bucket_for_time(dt)

            production_w = m.get("production")
            consumption_w = m.get("consumption")

            pd = m.get("productionDistribution") or {}
            cd = m.get("consumptionDistribution") or {}

            solar_to_home_w = pd.get("productionToHome")
            solar_to_grid_w = pd.get("productionToGrid")
            grid_to_home_w = cd.get("consumptionFromGrid")

            production_kwh = watts_to_kwh_15min(production_w)
            consumption_kwh = watts_to_kwh_15min(consumption_w)
            solar_to_home_kwh = watts_to_kwh_15min(solar_to_home_w)
            solar_to_grid_kwh = watts_to_kwh_15min(solar_to_grid_w)
            grid_to_home_kwh = watts_to_kwh_15min(grid_to_home_w)

            import_cost = grid_to_home_kwh * RATES[bucket]

            cur.execute("""
            INSERT OR REPLACE INTO readings (
                household_id,
                measurement_time, tariff_bucket,
                production_w, consumption_w, solar_to_home_w, solar_to_grid_w, grid_to_home_w,
                production_kwh, consumption_kwh, solar_to_home_kwh, solar_to_grid_kwh, grid_to_home_kwh,
                import_cost
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                DEFAULT_HOUSEHOLD_ID,
                ts, bucket,
                production_w, consumption_w, solar_to_home_w, solar_to_grid_w, grid_to_home_w,
                production_kwh, consumption_kwh, solar_to_home_kwh, solar_to_grid_kwh, grid_to_home_kwh,
                import_cost
            ))

            inserted += 1

    conn.commit()

    print(f"Imported/updated {inserted} readings")
    print()

    rows = cur.execute("""
    SELECT
        substr(measurement_time, 1, 10) AS day,
        tariff_bucket,
        ROUND(SUM(grid_to_home_kwh), 3) AS grid_import_kwh,
        ROUND(SUM(import_cost), 2) AS import_cost
    FROM readings
    GROUP BY day, tariff_bucket
    ORDER BY day, tariff_bucket
    """).fetchall()

    print("Daily Synergy EV Add On import cost:")
    for day, bucket, kwh, cost in rows:
        print(f"{day}  {bucket:16} {kwh:8.3f} kWh   ${cost:.2f}")

    print()
    daily = cur.execute("""
    SELECT
        substr(measurement_time, 1, 10) AS day,
        ROUND(SUM(grid_to_home_kwh), 3),
        ROUND(SUM(import_cost) + ?, 2)
    FROM readings
    GROUP BY day
    ORDER BY day
    """, (SUPPLY_CHARGE_PER_DAY,)).fetchall()

    print("Daily total including supply charge:")
    for day, kwh, cost in daily:
        print(f"{day}  {kwh:8.3f} kWh   ${cost:.2f}")

    conn.close()


if __name__ == "__main__":
    main()
