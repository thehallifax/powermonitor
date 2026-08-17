#!/usr/bin/env python3
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "power.sqlite"
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
SECRETS_PATH = PROJECT_ROOT / "config" / "secrets.json"
LOG_PATH = PROJECT_ROOT / "logs" / "ha_ocpp.log"
SOURCE = "home_assistant_ocpp"
DEFAULT_HOUSEHOLD_ID = "default_household"

DEFAULT_HOME_ASSISTANT_URL = "http://192.168.4.96:8123"
ENTITIES = {
    "transaction_id": "sensor.charger_transaction_id",
    "energy_session": "sensor.charger_energy_session",
    "power_active_import": "sensor.charger_power_active_import",
    "current_import": "sensor.charger_current_import",
    "voltage": "sensor.charger_voltage",
    "status_connector": "sensor.charger_status_connector",
    "time_session": "sensor.charger_time_session",
}
BUCKET_ORDER = ("overnight", "off_peak", "super_off_peak", "peak")
DEFAULT_IMPORT_RATES = {
    "super_off_peak": 0.086151,
    "overnight": 0.193841,
    "off_peak": 0.236916,
    "peak": 0.538446,
}


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def home_assistant_url() -> str:
    settings = load_json(SETTINGS_PATH)
    return str(settings.get("home_assistant_url") or DEFAULT_HOME_ASSISTANT_URL).rstrip("/")


def home_assistant_token() -> str:
    for name in ("HOME_ASSISTANT_TOKEN", "HA_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    secrets = load_json(SECRETS_PATH)
    return str(secrets.get("home_assistant_token") or secrets.get("ha_token") or "")


def import_rates() -> dict:
    settings = load_json(SETTINGS_PATH)
    configured = settings.get("import_rates") if isinstance(settings.get("import_rates"), dict) else {}
    return {
        bucket: float(configured.get(bucket, DEFAULT_IMPORT_RATES[bucket]))
        for bucket in BUCKET_ORDER
    }


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


def fetch_state(base_url: str, token: str, entity_id: str) -> dict:
    url = urljoin(base_url + "/", f"api/states/{entity_id}")
    request = Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Home Assistant returned invalid state for {entity_id}")
    return payload


def state_value(state: dict) -> str:
    return str(state.get("state") or "").strip()


def parse_float(value: str) -> Optional[float]:
    if not value or value.lower() in {"unknown", "unavailable", "none"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def normalize_energy_kwh(state: dict) -> float:
    value = parse_float(state_value(state))
    if value is None:
        return 0.0
    unit = str((state.get("attributes") or {}).get("unit_of_measurement") or "").lower()
    if unit in {"wh", "w h"}:
        return value / 1000.0
    return value


def normalize_power_kw(state: dict) -> float:
    value = parse_float(state_value(state))
    if value is None:
        return 0.0
    unit = str((state.get("attributes") or {}).get("unit_of_measurement") or "").lower()
    if unit == "w" or value > 100:
        return value / 1000.0
    return value


def parse_duration(value: str) -> Optional[timedelta]:
    value = value.strip()
    if not value or value.lower() in {"unknown", "unavailable", "none"}:
        return None
    parts = value.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return timedelta(hours=float(hours), minutes=float(minutes), seconds=float(seconds))
        if len(parts) == 2:
            minutes, seconds = parts
            return timedelta(minutes=float(minutes), seconds=float(seconds))
        return timedelta(seconds=float(value))
    except ValueError:
        return None


def ensure_ev_sessions(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ev_sessions (
            household_id TEXT DEFAULT 'default_household',
            start_time TEXT NOT NULL,
            end_time TEXT,
            energy_kwh REAL NOT NULL,
            source TEXT NOT NULL,
            cost_estimate REAL,
            transaction_id TEXT,
            last_seen TEXT,
            max_power_kw REAL,
            status TEXT,
            tariff_breakdown_json TEXT,
            first_seen_energy_kwh REAL,
            last_energy_kwh REAL,
            duration_minutes REAL,
            cost_partial INTEGER DEFAULT 0
        )
        """
    )
    present = {row[1] for row in conn.execute("PRAGMA table_info(ev_sessions)")}
    columns = {
        "household_id": "ALTER TABLE ev_sessions ADD COLUMN household_id TEXT",
        "end_time": "ALTER TABLE ev_sessions ADD COLUMN end_time TEXT",
        "cost_estimate": "ALTER TABLE ev_sessions ADD COLUMN cost_estimate REAL",
        "transaction_id": "ALTER TABLE ev_sessions ADD COLUMN transaction_id TEXT",
        "last_seen": "ALTER TABLE ev_sessions ADD COLUMN last_seen TEXT",
        "max_power_kw": "ALTER TABLE ev_sessions ADD COLUMN max_power_kw REAL",
        "status": "ALTER TABLE ev_sessions ADD COLUMN status TEXT",
        "tariff_breakdown_json": "ALTER TABLE ev_sessions ADD COLUMN tariff_breakdown_json TEXT",
        "first_seen_energy_kwh": "ALTER TABLE ev_sessions ADD COLUMN first_seen_energy_kwh REAL",
        "last_energy_kwh": "ALTER TABLE ev_sessions ADD COLUMN last_energy_kwh REAL",
        "duration_minutes": "ALTER TABLE ev_sessions ADD COLUMN duration_minutes REAL",
        "cost_partial": "ALTER TABLE ev_sessions ADD COLUMN cost_partial INTEGER DEFAULT 0",
    }
    for column, statement in columns.items():
        if column not in present:
            conn.execute(statement)
    conn.execute(
        "UPDATE ev_sessions SET household_id = ? WHERE household_id IS NULL OR household_id = ''",
        (DEFAULT_HOUSEHOLD_ID,),
    )
    conn.execute("DROP INDEX IF EXISTS idx_ev_sessions_transaction_id")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ev_sessions_household_transaction_id
        ON ev_sessions(household_id, transaction_id)
        WHERE transaction_id IS NOT NULL
        """
    )
    conn.commit()


def derive_start_time(existing_start: Optional[str], now: datetime, time_state: dict) -> str:
    if existing_start:
        return existing_start
    duration = parse_duration(state_value(time_state))
    if duration:
        return (now - duration).astimezone().isoformat(timespec="seconds")
    raw_changed = time_state.get("last_changed")
    if isinstance(raw_changed, str) and raw_changed:
        try:
            return datetime.fromisoformat(raw_changed.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
        except ValueError:
            pass
    return now.astimezone().isoformat(timespec="seconds")


def empty_breakdown() -> dict:
    return {bucket: {"kwh": 0.0, "cost": 0.0} for bucket in BUCKET_ORDER}


def load_breakdown(raw: Optional[str]) -> dict:
    if not raw:
        return empty_breakdown()
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return empty_breakdown()
    breakdown = empty_breakdown()
    if isinstance(loaded, dict):
        for bucket in BUCKET_ORDER:
            values = loaded.get(bucket, {})
            if isinstance(values, dict):
                breakdown[bucket]["kwh"] = float(values.get("kwh") or 0)
                breakdown[bucket]["cost"] = float(values.get("cost") or 0)
    return breakdown


def duration_minutes(start_time: str, last_seen: str) -> float:
    try:
        start = datetime.fromisoformat(start_time)
        end = datetime.fromisoformat(last_seen)
    except ValueError:
        return 0.0
    return max((end - start).total_seconds() / 60, 0.0)


def import_snapshot(states: dict) -> tuple[str, float, float, str, float, float, bool]:
    transaction_id = state_value(states["transaction_id"])
    if not transaction_id or transaction_id.lower() in {"unknown", "unavailable", "none", "0"}:
        raise ValueError("No active Home Assistant OCPP transaction_id")

    energy_kwh = normalize_energy_kwh(states["energy_session"])
    power_kw = normalize_power_kw(states["power_active_import"])
    status = state_value(states["status_connector"]) or "unknown"
    now = datetime.now().astimezone()
    last_seen = now.isoformat(timespec="seconds")
    rates = import_rates()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        ensure_ev_sessions(conn)
        existing = conn.execute(
            """
            SELECT
                start_time, max_power_kw, last_energy_kwh, energy_kwh,
                tariff_breakdown_json, cost_estimate, first_seen_energy_kwh,
                cost_partial
            FROM ev_sessions
            WHERE household_id = ? AND transaction_id = ? AND source = ?
            """,
            (DEFAULT_HOUSEHOLD_ID, transaction_id, SOURCE),
        ).fetchone()
        start_time = now.astimezone().isoformat(timespec="seconds") if not existing else existing[0]
        max_power_kw = max(float(existing[1] or 0) if existing else 0.0, power_kw)
        previous_energy = None
        if existing:
            previous_energy = existing[2] if existing[2] is not None else existing[3]
        first_seen_energy = float(existing[6]) if existing and existing[6] is not None else energy_kwh
        breakdown = load_breakdown(existing[4] if existing else None)
        cost_estimate = float(existing[5] or 0) if existing else 0.0
        legacy_uncosted_session = bool(
            existing
            and existing[2] is None
            and existing[3] is not None
            and float(existing[3] or 0) > 0
            and cost_estimate <= 0
        )
        cost_partial = bool(existing[7]) if existing else energy_kwh > 0
        if legacy_uncosted_session:
            cost_partial = True
        delta_kwh = 0.0

        if previous_energy is not None:
            delta_kwh = energy_kwh - float(previous_energy)
            if delta_kwh < 0:
                delta_kwh = 0.0
                first_seen_energy = energy_kwh
                cost_partial = True
        elif energy_kwh > 0:
            cost_partial = True

        if delta_kwh > 0:
            bucket = bucket_for_time(now)
            delta_cost = delta_kwh * rates[bucket]
            breakdown[bucket]["kwh"] += delta_kwh
            breakdown[bucket]["cost"] += delta_cost
            cost_estimate += delta_cost

        duration = duration_minutes(start_time, last_seen)
        conn.execute(
            """
            INSERT INTO ev_sessions (
                household_id, transaction_id, start_time, last_seen, end_time, energy_kwh,
                max_power_kw, status, source, cost_estimate, tariff_breakdown_json,
                first_seen_energy_kwh, last_energy_kwh, duration_minutes, cost_partial
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(household_id, transaction_id) WHERE transaction_id IS NOT NULL DO UPDATE SET
                last_seen = excluded.last_seen,
                end_time = excluded.end_time,
                energy_kwh = excluded.energy_kwh,
                max_power_kw = MAX(COALESCE(ev_sessions.max_power_kw, 0), excluded.max_power_kw),
                status = excluded.status,
                source = excluded.source,
                cost_estimate = excluded.cost_estimate,
                tariff_breakdown_json = excluded.tariff_breakdown_json,
                first_seen_energy_kwh = excluded.first_seen_energy_kwh,
                last_energy_kwh = excluded.last_energy_kwh,
                duration_minutes = excluded.duration_minutes,
                cost_partial = excluded.cost_partial
            """,
            (
                DEFAULT_HOUSEHOLD_ID,
                transaction_id,
                start_time,
                last_seen,
                last_seen,
                energy_kwh,
                max_power_kw,
                status,
                SOURCE,
                cost_estimate,
                json.dumps(breakdown, sort_keys=True),
                first_seen_energy,
                energy_kwh,
                duration,
                1 if cost_partial else 0,
            ),
        )
        conn.commit()
    return transaction_id, energy_kwh, max_power_kw, status, delta_kwh, cost_estimate, cost_partial


def main() -> int:
    setup_logging()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        ensure_ev_sessions(conn)

    access_key = home_assistant_token()
    if not access_key:
        logging.error("Home Assistant token is not configured")
        print("Home Assistant token is not configured")
        return 1

    base_url = home_assistant_url()
    try:
        states = {
            name: fetch_state(base_url, access_key, entity_id)
            for name, entity_id in ENTITIES.items()
        }
        transaction_id, energy_kwh, max_power_kw, status, delta_kwh, cost_estimate, cost_partial = import_snapshot(states)
    except HTTPError as exc:
        logging.error("Home Assistant API returned HTTP %s", exc.code)
        print(f"Home Assistant API returned HTTP {exc.code}")
        return 1
    except (URLError, TimeoutError) as exc:
        logging.error("Home Assistant network failure: %s", exc)
        print("Home Assistant network failure")
        return 1
    except Exception as exc:
        logging.error("Home Assistant OCPP import failed: %s", exc)
        print(f"Home Assistant OCPP import failed: {exc}")
        return 1

    logging.info(
        "Imported Home Assistant OCPP snapshot for transaction %s: %.3f kWh, %.3f kW max, status %s, delta %.3f kWh, cost %.4f, partial %s",
        transaction_id,
        energy_kwh,
        max_power_kw,
        status,
        delta_kwh,
        cost_estimate,
        cost_partial,
    )
    print(
        f"Imported Home Assistant OCPP session {transaction_id}: "
        f"{energy_kwh:.3f} kWh, delta {delta_kwh:.3f} kWh, "
        f"cost ${cost_estimate:.2f}, {max_power_kw:.3f} kW max, status {status}"
        f"{' (partial session cost)' if cost_partial else ''}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
