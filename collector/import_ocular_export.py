#!/usr/bin/env python3
import csv
import json
import logging
import re
import sqlite3
from contextlib import closing
import sys
import zipfile
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.import_framework import (
    ImportPreview,
    ImportResult,
    ImportSource,
    ImportValidationIssue,
)
from engine.ev_charging_inference import (
    EVChargingInferenceConfig,
    infer_active_charging_interval,
    split_energy_by_tariff,
)
from engine.models import HouseholdContext
from engine.data_engine import DataEngine
from engine.provider_framework import (
    ProviderCapability,
    ProviderFreshness,
    ProviderObservation,
    ProviderObservationQuality,
    ProviderObservationType,
    ProviderType,
)

DB_PATH = PROJECT_ROOT / "data" / "power.sqlite"
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
LOG_PATH = PROJECT_ROOT / "logs" / "ocular_export.log"
SOURCE = "ocular_export"
DEFAULT_HOUSEHOLD_ID = "default_household"
BUCKET_ORDER = ("overnight", "off_peak", "super_off_peak", "peak")
DEFAULT_IMPORT_RATES = {
    "super_off_peak": 0.086151,
    "overnight": 0.193841,
    "off_peak": 0.236916,
    "peak": 0.538446,
}
TARIFF_COLUMN_PAIRS = {
    bucket: (f"{bucket}_kwh", f"{bucket}_cost")
    for bucket in BUCKET_ORDER
}
REQUIRED_COLUMNS = {
    "record_number": ["Record number"],
    "charger_number": ["Charger number"],
    "energy_kwh": ["Charging amount", "Charging amount (kWh)"],
    "start_time": ["Charging start time"],
    "end_time": ["Charging stop time"],
    "duration": ["Charging duration", "Charging duration (min)"],
    "starting_user": ["Starting user"],
    "stopping_user": ["Stopping user"],
}
NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


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


def next_bucket_boundary(dt: datetime) -> datetime:
    day = dt.date()
    boundaries = [
        datetime.combine(day, time(6, 0), tzinfo=dt.tzinfo),
        datetime.combine(day, time(9, 0), tzinfo=dt.tzinfo),
        datetime.combine(day, time(15, 0), tzinfo=dt.tzinfo),
        datetime.combine(day, time(21, 0), tzinfo=dt.tzinfo),
        datetime.combine(day, time(23, 0), tzinfo=dt.tzinfo),
        datetime.combine(day + timedelta(days=1), time(6, 0), tzinfo=dt.tzinfo),
    ]
    return min(boundary for boundary in boundaries if boundary > dt)


def inference_config() -> EVChargingInferenceConfig:
    return EVChargingInferenceConfig.from_settings(settings_path=SETTINGS_PATH)


def split_tariff_cost(start: Optional[datetime], end: Optional[datetime], energy_kwh: float) -> tuple[float, dict]:
    return split_energy_by_tariff(start, end, energy_kwh, import_rates())


def validate_breakdown_totals(transaction_id: str, energy_kwh: float, total_cost: float, breakdown: dict) -> None:
    breakdown_kwh = sum(float(values.get("kwh") or 0) for values in breakdown.values())
    breakdown_cost = sum(float(values.get("cost") or 0) for values in breakdown.values())
    if abs(breakdown_kwh - energy_kwh) > 0.01:
        raise ValueError(
            f"Ocular record {transaction_id} tariff kWh total {breakdown_kwh:.3f} "
            f"does not match session energy {energy_kwh:.3f}"
        )
    if abs(breakdown_cost - total_cost) > 0.01:
        raise ValueError(
            f"Ocular record {transaction_id} tariff cost total {breakdown_cost:.3f} "
            f"does not match session cost {total_cost:.3f}"
        )


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
            cost_partial INTEGER DEFAULT 0,
            start_source TEXT,
            stop_source TEXT,
            notes_json TEXT,
            super_off_peak_kwh REAL DEFAULT 0,
            super_off_peak_cost REAL DEFAULT 0,
            overnight_kwh REAL DEFAULT 0,
            overnight_cost REAL DEFAULT 0,
            off_peak_kwh REAL DEFAULT 0,
            off_peak_cost REAL DEFAULT 0,
            peak_kwh REAL DEFAULT 0,
            peak_cost REAL DEFAULT 0
        )
        """
    )
    DataEngine._ensure_ev_sessions_nullable_end_time(conn)
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
        "start_source": "ALTER TABLE ev_sessions ADD COLUMN start_source TEXT",
        "stop_source": "ALTER TABLE ev_sessions ADD COLUMN stop_source TEXT",
        "notes_json": "ALTER TABLE ev_sessions ADD COLUMN notes_json TEXT",
    }
    for bucket, (kwh_column, cost_column) in TARIFF_COLUMN_PAIRS.items():
        columns[kwh_column] = f"ALTER TABLE ev_sessions ADD COLUMN {kwh_column} REAL DEFAULT 0"
        columns[cost_column] = f"ALTER TABLE ev_sessions ADD COLUMN {cost_column} REAL DEFAULT 0"
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


def col_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + ord(char.upper()) - ord("A") + 1
    return index - 1


def shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    values = []
    for item in root.findall("main:si", NS):
        text = "".join(node.text or "" for node in item.findall(".//main:t", NS))
        values.append(text)
    return values


def first_sheet_path(zf: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    first_sheet = workbook.find("main:sheets/main:sheet", NS)
    if first_sheet is None:
        raise ValueError("Workbook has no sheets")
    rel_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for rel in rels:
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib["Target"]
            return "xl/" + target.lstrip("/")
    return "xl/worksheets/sheet1.xml"


def cell_value(cell: ET.Element, strings: list[str]):
    cell_type = cell.attrib.get("t")
    value_node = cell.find("main:v", NS)
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", NS))
    if value_node is None:
        return ""
    raw = value_node.text or ""
    if cell_type == "s":
        return strings[int(raw)]
    return raw


def read_xlsx(path: Path) -> list[list]:
    with zipfile.ZipFile(path) as zf:
        strings = shared_strings(zf)
        sheet = ET.fromstring(zf.read(first_sheet_path(zf)))
    rows = []
    for row in sheet.findall(".//main:sheetData/main:row", NS):
        values = []
        for cell in row.findall("main:c", NS):
            index = col_index(cell.attrib.get("r", "A1"))
            while len(values) <= index:
                values.append("")
            values[index] = cell_value(cell, strings)
        rows.append(values)
    return rows


def normalize_header(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def parse_float(value) -> float:
    text = str(value or "").replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    return float(match.group(0))


def excel_serial_to_datetime(value: float) -> datetime:
    return datetime(1899, 12, 30) + timedelta(days=value)


def parse_datetime_value(value) -> datetime:
    if isinstance(value, (int, float)):
        return excel_serial_to_datetime(float(value)).astimezone()
    text = str(value or "").strip()
    if not text:
        raise ValueError("Missing datetime")
    try:
        return excel_serial_to_datetime(float(text)).astimezone()
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt).astimezone()
        except ValueError:
            continue
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()


def parse_duration_minutes(value, start: datetime, end: Optional[datetime]) -> float:
    text = str(value or "").strip()
    if text:
        parts = text.split(":")
        try:
            if len(parts) == 3:
                return float(parts[0]) * 60 + float(parts[1]) + float(parts[2]) / 60
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            number = float(text)
            if 0 < number < 10:
                return number * 24 * 60
            return number
        except ValueError:
            pass
    if end is None:
        return 0.0
    return max((end - start).total_seconds() / 60, 0.0)


def row_dicts(rows: list[list]) -> list[dict]:
    if not rows:
        return []
    header = [normalize_header(value) for value in rows[0]]
    positions = {}
    for key, aliases in REQUIRED_COLUMNS.items():
        normalized_aliases = [normalize_header(alias) for alias in aliases]
        for normalized in normalized_aliases:
            if normalized in header:
                positions[key] = header.index(normalized)
                break
        else:
            raise ValueError(f"Missing Ocular column: {' or '.join(aliases)}")
    records = []
    for row in rows[1:]:
        if not any(str(value or "").strip() for value in row):
            continue
        records.append({key: row[index] if index < len(row) else "" for key, index in positions.items()})
    return records


OCULAR_IMPORT_SOURCE = ImportSource(
    source_id=SOURCE,
    display_name="Ocular EV charging history",
    supported_extensions=(".xlsx", ".xlsm"),
    description="Ocular charger history workbook export",
)

OCULAR_CSV_IMPORT_SOURCE = ImportSource(
    source_id="ocular_csv",
    display_name="Ocular EV charging CSV",
    supported_extensions=(".csv",),
    description="Ocular charger session CSV export",
)

CSV_COLUMNS = {
    "record_number": ("Transaction ID", "Record number"),
    "start_time": ("Charging start time", "Start time"),
    "end_time": ("Charging stop time", "End time"),
    "energy_kwh": ("Charging amount (kWh)", "Energy (kWh)"),
}
CSV_DURATION_COLUMNS = ("Charging duration (min)", "Duration (min)")


def import_record(
    conn: sqlite3.Connection,
    record: dict,
    household_id: str = DEFAULT_HOUSEHOLD_ID,
    source: str = SOURCE,
) -> bool:
    transaction_id = str(record["record_number"]).strip()
    if not transaction_id:
        return False
    connected_at = parse_datetime_value(record["start_time"])
    disconnected_at = parse_optional_datetime(record.get("end_time"))
    if disconnected_at and disconnected_at < connected_at:
        raise ValueError(f"Ocular record {transaction_id} stop time is before start time")
    energy_kwh = parse_float(record["energy_kwh"])
    connected_duration = parse_duration_minutes(record["duration"], connected_at, disconnected_at)
    config = inference_config()
    estimate = infer_active_charging_interval(
        connected_at,
        disconnected_at,
        energy_kwh,
        config,
        connected_duration,
    )
    cost, breakdown = split_tariff_cost(estimate.active_start, estimate.active_finish, energy_kwh)
    cost = sum(float(values.get("cost") or 0) for values in breakdown.values())
    if estimate.active_start and estimate.active_finish:
        validate_breakdown_totals(transaction_id, energy_kwh, cost, breakdown)
    max_power_kw = config.charger_power_kw if estimate.inferred else None
    cost_partial = 0 if estimate.active_start and estimate.active_finish else 1
    status = "Completed" if estimate.active_start and estimate.active_finish else "Incomplete"
    notes = {
        "record_number": transaction_id,
        "charger_number": str(record["charger_number"]).strip(),
        "connected_at": connected_at.isoformat(timespec="seconds"),
        "disconnected_at": disconnected_at.isoformat(timespec="seconds") if disconnected_at else None,
        "connected_duration": str(record["duration"]).strip(),
        "connected_duration_minutes": connected_duration,
        # Compatibility for older report/debug consumers. This is a connected duration from Ocular.
        "charging_duration": str(record["duration"]).strip(),
        **estimate.as_notes(),
    }
    tariff_columns = []
    tariff_placeholders = []
    tariff_values = []
    tariff_updates = []
    for bucket in BUCKET_ORDER:
        kwh_column, cost_column = TARIFF_COLUMN_PAIRS[bucket]
        tariff_columns.extend((kwh_column, cost_column))
        tariff_placeholders.extend(("?", "?"))
        tariff_values.extend((
            float(breakdown[bucket]["kwh"]),
            float(breakdown[bucket]["cost"]),
        ))
        tariff_updates.extend((
            f"{kwh_column} = excluded.{kwh_column}",
            f"{cost_column} = excluded.{cost_column}",
        ))
    conn.execute(
        f"""
        INSERT INTO ev_sessions (
            household_id, transaction_id, start_time, end_time, last_seen, energy_kwh,
            source, cost_estimate, tariff_breakdown_json, first_seen_energy_kwh,
            last_energy_kwh, duration_minutes, cost_partial, status,
            max_power_kw, start_source, stop_source, notes_json,
            {", ".join(tariff_columns)}
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {", ".join(tariff_placeholders)})
        ON CONFLICT(household_id, transaction_id) WHERE transaction_id IS NOT NULL DO UPDATE SET
            start_time = excluded.start_time,
            end_time = excluded.end_time,
            last_seen = excluded.last_seen,
            energy_kwh = excluded.energy_kwh,
            source = excluded.source,
            cost_estimate = excluded.cost_estimate,
            tariff_breakdown_json = excluded.tariff_breakdown_json,
            first_seen_energy_kwh = excluded.first_seen_energy_kwh,
            last_energy_kwh = excluded.last_energy_kwh,
            duration_minutes = excluded.duration_minutes,
            cost_partial = excluded.cost_partial,
            status = excluded.status,
            max_power_kw = excluded.max_power_kw,
            start_source = excluded.start_source,
            stop_source = excluded.stop_source,
            notes_json = excluded.notes_json,
            {", ".join(tariff_updates)}
        """,
        (
            household_id,
            transaction_id,
            connected_at.isoformat(timespec="seconds"),
            disconnected_at.isoformat(timespec="seconds") if disconnected_at else None,
            (disconnected_at or connected_at).isoformat(timespec="seconds"),
            energy_kwh,
            source,
            cost,
            json.dumps(breakdown, sort_keys=True),
            0.0,
            energy_kwh,
            connected_duration,
            cost_partial,
            status,
            max_power_kw,
            str(record["starting_user"]).strip(),
            str(record["stopping_user"]).strip(),
            json.dumps(notes, sort_keys=True),
            *tariff_values,
        ),
    )
    return True


def parse_file(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        return csv_records(path)
    return row_dicts(read_xlsx(path))


def csv_records(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("Ocular CSV is missing a header row")
        headers = {normalize_header(name): name for name in reader.fieldnames if name}
        positions = {}
        for key, aliases in CSV_COLUMNS.items():
            for alias in aliases:
                original = headers.get(normalize_header(alias))
                if original:
                    positions[key] = original
                    break
            else:
                raise ValueError(f"Missing Ocular CSV column: {' or '.join(aliases)}")
        duration_header = next(
            (headers[normalize_header(alias)] for alias in CSV_DURATION_COLUMNS if normalize_header(alias) in headers),
            None,
        )
        records = []
        for row in reader:
            if not any(str(value or "").strip() for value in row.values()):
                continue
            records.append({
                **{key: row.get(header, "") for key, header in positions.items()},
                "duration": row.get(duration_header, "") if duration_header else "",
                "charger_number": "",
                "starting_user": "",
                "stopping_user": "",
            })
    return records


def parse_optional_datetime(value) -> Optional[datetime]:
    if str(value or "").strip() == "":
        return None
    try:
        return parse_datetime_value(value)
    except ValueError:
        return None


def validate_records(records: list[dict]) -> list[ImportValidationIssue]:
    issues = []
    seen_ids = set()
    for index, record in enumerate(records, start=2):
        transaction_id = str(record.get("record_number") or "").strip()
        if not transaction_id:
            issues.append(ImportValidationIssue("error", "Missing record number", row=index, field="record_number"))
            continue
        if transaction_id in seen_ids:
            issues.append(ImportValidationIssue("warning", "Duplicate record number in upload", row=index, field="record_number"))
        seen_ids.add(transaction_id)
        try:
            start = parse_datetime_value(record.get("start_time"))
            end = parse_optional_datetime(record.get("end_time"))
            if end and end < start:
                raise ValueError("Stop time is before start time")
            energy_kwh = parse_float(record.get("energy_kwh"))
            duration = parse_duration_minutes(record.get("duration"), start, end)
            estimate = infer_active_charging_interval(start, end, energy_kwh, inference_config(), duration)
            cost, breakdown = split_tariff_cost(estimate.active_start, estimate.active_finish, energy_kwh)
            if estimate.active_start and estimate.active_finish:
                validate_breakdown_totals(transaction_id, energy_kwh, cost, breakdown)
            if end is None:
                message = "Missing disconnection time; active interval cannot be completed"
                if str(record.get("end_time") or "").strip():
                    message = "Invalid disconnection time; active interval cannot be completed"
                issues.append(ImportValidationIssue("warning", message, row=index, field="end_time"))
            if energy_kwh <= 0:
                issues.append(ImportValidationIssue("warning", "Charging amount is zero", row=index, field="energy_kwh"))
            if duration <= 0:
                issues.append(ImportValidationIssue("warning", "Connected duration is zero or unavailable", row=index, field="duration"))
        except (TypeError, ValueError) as exc:
            issues.append(ImportValidationIssue("error", f"Invalid Ocular row: {exc}", row=index))
    return issues


def preview_records(
    records: list[dict],
    issues: Optional[list[ImportValidationIssue]] = None,
    source: ImportSource = OCULAR_IMPORT_SOURCE,
) -> ImportPreview:
    issues = issues or validate_records(records)
    error_rows = {issue.row for issue in issues if issue.severity == "error" and issue.row is not None}
    starts = []
    ends = []
    total_energy = 0.0
    samples = []
    for index, record in enumerate(records, start=2):
        if index in error_rows:
            continue
        try:
            start = parse_datetime_value(record.get("start_time"))
            end = parse_optional_datetime(record.get("end_time"))
        except (TypeError, ValueError):
            continue
        starts.append(start)
        if end:
            ends.append(end)
        energy = parse_float(record.get("energy_kwh"))
        total_energy += energy
        if len(samples) < 3:
            samples.append(
                {
                    "record_number": str(record.get("record_number") or "").strip(),
                    "start_time": start.isoformat(timespec="seconds"),
                    "end_time": end.isoformat(timespec="seconds") if end else None,
                    "energy_kwh": round(energy, 3),
                }
            )
    return ImportPreview(
        source=source,
        total_records=len(records),
        valid_records=max(len(records) - len(error_rows), 0),
        date_start=min(starts).date().isoformat() if starts else None,
        date_end=max(ends).date().isoformat() if ends else None,
        total_energy_kwh=round(total_energy, 3),
        issues=issues,
        sample_records=samples,
    )


def import_records_to_db(
    records: list[dict],
    db_path: Path = DB_PATH,
    household_id: str = DEFAULT_HOUSEHOLD_ID,
    source: str = SOURCE,
) -> tuple[int, int]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        ensure_ev_sessions(conn)
        before = conn.execute(
            "SELECT COUNT(*) FROM ev_sessions WHERE household_id = ? AND source = ?",
            (household_id, source),
        ).fetchone()[0]
        imported = 0
        for record in records:
            if import_record(conn, record, household_id=household_id, source=source):
                imported += 1
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM ev_sessions WHERE household_id = ? AND source = ?",
            (household_id, source),
        ).fetchone()[0]
    return imported, after - before


def import_file(path: Path, household_id: str = DEFAULT_HOUSEHOLD_ID) -> tuple[int, int]:
    source = OCULAR_CSV_IMPORT_SOURCE.source_id if path.suffix.lower() == ".csv" else SOURCE
    return import_records_to_db(parse_file(path), DB_PATH, household_id, source=source)


class OcularEVImporter:
    """Importer adapter for existing Ocular EV charging history workbooks."""

    source = OCULAR_IMPORT_SOURCE

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = Path(db_path)

    def detect(self, file_path: Path) -> bool:
        if file_path.suffix.lower() not in self.source.supported_extensions:
            return False
        try:
            parse_file(file_path)
        except (KeyError, OSError, ValueError, zipfile.BadZipFile, ET.ParseError):
            return False
        return True

    def parse(self, file_path: Path) -> list[dict]:
        return parse_file(file_path)

    def validate(self, parsed_data: list[dict]) -> list[ImportValidationIssue]:
        return validate_records(parsed_data)

    def preview(self, parsed_data: list[dict]) -> ImportPreview:
        return preview_records(parsed_data, source=self.source)

    def commit(
        self,
        parsed_data: list[dict],
        household_context: HouseholdContext,
        job_id: Optional[str] = None,
    ) -> ImportResult:
        issues = validate_records(parsed_data)
        if any(issue.severity == "error" for issue in issues):
            return ImportResult(
                job_id=job_id or "",
                source=self.source,
                status="failed",
                processed_records=len(parsed_data),
                issues=issues,
                message="Import contains validation errors.",
            )
        household_id = household_context.household.household_id
        processed, inserted = import_records_to_db(parsed_data, self.db_path, household_id, self.source.source_id)
        observations = ocular_provider_observations(parsed_data, household_id, job_id, self.source.source_id)
        DataEngine(db_path=self.db_path, household_context=household_context).persist_provider_observations(observations)
        return ImportResult(
            job_id=job_id or "",
            source=self.source,
            status="completed",
            processed_records=processed,
            inserted_records=inserted,
            updated_records=max(processed - inserted, 0),
            issues=issues,
            message=f"Imported {processed} Ocular charging sessions.",
        )


class OcularCSVImporter(OcularEVImporter):
    """Ocular session CSV importer sharing the established session pipeline."""

    source = OCULAR_CSV_IMPORT_SOURCE

    def detect(self, file_path: Path) -> bool:
        if file_path.suffix.lower() != ".csv":
            return False
        try:
            csv_records(file_path)
        except (OSError, ValueError, csv.Error):
            return False
        return True

    def validate(self, parsed_data: list[dict]) -> list[ImportValidationIssue]:
        issues = super().validate(parsed_data)
        return [
            ImportValidationIssue("error", issue.message, issue.row, issue.field)
            if issue.field == "end_time" and issue.severity == "warning"
            else issue
            for issue in issues
        ]

    def preview(self, parsed_data: list[dict]) -> ImportPreview:
        return preview_records(parsed_data, self.validate(parsed_data), self.source)


def ocular_provider_observations(
    records: list[dict],
    household_id: str,
    job_id: Optional[str] = None,
    source: str = SOURCE,
) -> list[ProviderObservation]:
    observations = []
    received_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for record in records:
        transaction_id = str(record.get("record_number") or "").strip()
        try:
            connected_at = parse_datetime_value(record.get("start_time"))
            disconnected_at = parse_optional_datetime(record.get("end_time"))
            energy_kwh = parse_float(record.get("energy_kwh"))
            connected_duration = parse_duration_minutes(record.get("duration"), connected_at, disconnected_at)
            config = inference_config()
            estimate = infer_active_charging_interval(
                connected_at,
                disconnected_at,
                energy_kwh,
                config,
                connected_duration,
            )
            cost, breakdown = split_tariff_cost(estimate.active_start, estimate.active_finish, energy_kwh)
        except (TypeError, ValueError):
            continue
        observed_at = (disconnected_at or connected_at).isoformat(timespec="seconds")
        observations.append(
            ProviderObservation(
                ProviderCapability.EV_SESSIONS,
                "ev_session",
                value={
                    "transaction_id": transaction_id,
                    "energy_kwh": round(energy_kwh, 4),
                    "cost_estimate": round(cost, 4),
                    "duration_minutes": round(estimate.active_duration_minutes or 0.0, 2),
                    "connected_at": connected_at.isoformat(timespec="seconds"),
                    "disconnected_at": disconnected_at.isoformat(timespec="seconds") if disconnected_at else None,
                    "connected_duration_minutes": round(connected_duration, 2),
                    "estimated_active_start": estimate.active_start.isoformat(timespec="seconds") if estimate.active_start else None,
                    "estimated_active_finish": estimate.active_finish.isoformat(timespec="seconds") if estimate.active_finish else None,
                    "active_interval_inferred": estimate.inferred,
                    "inference_diagnostics": estimate.diagnostics,
                    "tariff_breakdown": breakdown,
                },
                unit="session",
                provider_id=source,
                provider_type=ProviderType.EV_CHARGER,
                household_id=household_id,
                observation_type=ProviderObservationType.EV_SESSION,
                observed_at=observed_at,
                received_at=received_at,
                freshness=ProviderFreshness(
                    state="fresh",
                    observed_at=observed_at,
                    note="Imported Ocular EV charging session.",
                ),
                quality=ProviderObservationQuality.HIGH,
                provenance="Ocular EV charging history import",
                raw_reference=transaction_id,
                import_job_id=job_id,
                note="Normalized EV session observation from Ocular export.",
            )
        )
    return observations


def main() -> int:
    setup_logging()
    if len(sys.argv) != 2:
        print("Usage: python3 collector/import_ocular_export.py /path/to/export.xlsx", file=sys.stderr)
        return 2
    path = Path(sys.argv[1]).expanduser()
    if not path.exists():
        print(f"Ocular export not found: {path}", file=sys.stderr)
        return 1
    try:
        imported, inserted = import_file(path)
    except Exception as exc:
        logging.error("Ocular export import failed: %s", exc)
        print(f"Ocular export import failed: {exc}", file=sys.stderr)
        return 1
    logging.info("Imported Ocular export %s: %d rows processed, %d new sessions", path, imported, inserted)
    print(f"Imported Ocular export: processed {imported} rows, inserted {inserted} new sessions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
