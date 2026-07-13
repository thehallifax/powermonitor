import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "power.sqlite"
DEFAULT_HOUSEHOLD_ID = "default_household"
LOCAL_TIMEZONE = ZoneInfo("Australia/Perth")
SOLAREDGE_PROVIDER_ID = "solaredge"
SOLAREDGE_SOURCE_ID = "solaredge_dashboard_power"
SOLAREDGE_SOURCE_RESOLUTION = "interval_15m"

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


def parse_measurement_time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


def is_non_zero_reading(*values):
    return any(abs(float(value or 0)) > 0.000001 for value in values)


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
    reading_metadata_columns = {
        "provider_id": "ALTER TABLE readings ADD COLUMN provider_id TEXT",
        "source_id": "ALTER TABLE readings ADD COLUMN source_id TEXT",
        "source_resolution": "ALTER TABLE readings ADD COLUMN source_resolution TEXT",
        "source_record_id": "ALTER TABLE readings ADD COLUMN source_record_id TEXT",
        "import_job_id": "ALTER TABLE readings ADD COLUMN import_job_id TEXT",
        "imported_at": "ALTER TABLE readings ADD COLUMN imported_at TEXT",
    }
    for column, statement in reading_metadata_columns.items():
        if column not in present:
            cur.execute(statement)
    cur.execute(
        "UPDATE readings SET household_id = ? WHERE household_id IS NULL OR household_id = ''",
        (DEFAULT_HOUSEHOLD_ID,),
    )

    received = 0
    imported = 0
    updated = 0
    duplicates_ignored = 0
    zero_payload_skipped = 0
    imported_times = []
    import_job_id = os.environ.get("POWERMONITOR_IMPORT_JOB_ID")
    imported_at = datetime.now(LOCAL_TIMEZONE).isoformat(timespec="seconds")

    for m in data.get("measurements", []):
        received += 1

        ts = m.get("measurementTime")

        if not ts:
            continue

        dt = parse_measurement_time(ts)
        normalized_ts = dt.isoformat()
        source_record_id = f"{SOLAREDGE_SOURCE_ID}:{normalized_ts}"

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
        incoming_non_zero = is_non_zero_reading(
            production_kwh,
            consumption_kwh,
            solar_to_home_kwh,
            solar_to_grid_kwh,
            grid_to_home_kwh,
        )

        cur.execute(
            """
            SELECT production_kwh, consumption_kwh, solar_to_home_kwh, solar_to_grid_kwh, grid_to_home_kwh
            FROM readings
            WHERE household_id = ? AND measurement_time = ?
            """,
            (DEFAULT_HOUSEHOLD_ID, normalized_ts),
        )
        existing = cur.fetchone()
        if existing:
            existing_non_zero = is_non_zero_reading(*existing)
            if existing_non_zero and not incoming_non_zero:
                zero_payload_skipped += 1
                continue
            if not existing_non_zero and incoming_non_zero:
                cur.execute("""
                UPDATE readings
                SET tariff_bucket = ?,
                    production_w = ?,
                    consumption_w = ?,
                    solar_to_home_w = ?,
                    solar_to_grid_w = ?,
                    grid_to_home_w = ?,
                    production_kwh = ?,
                    consumption_kwh = ?,
                    solar_to_home_kwh = ?,
                    solar_to_grid_kwh = ?,
                    grid_to_home_kwh = ?,
                    import_cost = ?,
                    provider_id = ?,
                    source_id = ?,
                    source_resolution = ?,
                    source_record_id = ?,
                    import_job_id = COALESCE(?, import_job_id),
                    imported_at = ?
                WHERE household_id = ? AND measurement_time = ?
                """, (
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
                    import_cost,
                    SOLAREDGE_PROVIDER_ID,
                    SOLAREDGE_SOURCE_ID,
                    SOLAREDGE_SOURCE_RESOLUTION,
                    source_record_id,
                    import_job_id,
                    imported_at,
                    DEFAULT_HOUSEHOLD_ID,
                    normalized_ts,
                ))
                updated += 1
                imported_times.append(dt)
                continue
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

            import_cost,
            provider_id,
            source_id,
            source_resolution,
            source_record_id,
            import_job_id,
            imported_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            DEFAULT_HOUSEHOLD_ID,
            normalized_ts,
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

            import_cost,
            SOLAREDGE_PROVIDER_ID,
            SOLAREDGE_SOURCE_ID,
            SOLAREDGE_SOURCE_RESOLUTION,
            source_record_id,
            import_job_id,
            imported_at,
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
        f"records updated {updated}; "
        f"duplicates ignored {duplicates_ignored}; "
        f"zero payload rows skipped {zero_payload_skipped}; "
        f"observations promoted {observations_promoted}"
    )
    print(f"Imported {imported} readings")
    print(f"Updated {updated} readings")

    conn.close()


if __name__ == "__main__":
    main()
