#!/usr/bin/env python3
"""Deterministic fictional demo data for PowerMonitor."""

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

from periods import LOCAL_TIMEZONE

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUCKET_RATES = {
    "super_off_peak": 0.086151,
    "overnight": 0.193841,
    "off_peak": 0.236916,
    "peak": 0.538446,
}
SUPPLY_CHARGE = 1.292269
EXPORT_RATE = 0.02


@dataclass(frozen=True)
class DemoEnvironment:
    db_path: Path
    settings_path: Path
    home_profile_path: Path
    report_path: Path
    collect_log_path: Path
    collector_health_path: Path
    weather_path: Path
    recommendations_path: Path


def demo_requested(argv: Optional[list[str]] = None, environ: Optional[dict] = None) -> bool:
    import os
    import sys

    argv = list(sys.argv if argv is None else argv)
    environ = os.environ if environ is None else environ
    value = str(environ.get("POWMONITOR_DEMO") or "").strip().lower()
    return value in {"1", "true", "yes", "on"} or "--demo" in argv


def ensure_demo_environment(
    project_root: Path = PROJECT_ROOT,
    today: Optional[date] = None,
) -> DemoEnvironment:
    today = today or datetime.now(LOCAL_TIMEZONE).date()
    data_dir = project_root / "data" / "demo"
    config_dir = project_root / "config" / "demo"
    report_dir = project_root / "reports" / "demo"
    log_dir = project_root / "logs" / "demo"
    for directory in (data_dir, config_dir, report_dir, log_dir):
        directory.mkdir(parents=True, exist_ok=True)

    env = DemoEnvironment(
        db_path=data_dir / "power.sqlite",
        settings_path=config_dir / "settings.json",
        home_profile_path=config_dir / "home_profile.json",
        report_path=report_dir / "index.html",
        collect_log_path=log_dir / "collect.log",
        collector_health_path=data_dir / "collector_health.json",
        weather_path=data_dir / "weather.json",
        recommendations_path=data_dir / "recommendations.json",
    )
    write_demo_settings(env.settings_path, today)
    write_demo_home_profile(env.home_profile_path)
    write_demo_weather(env.weather_path, today)
    write_demo_recommendations(env.recommendations_path)
    write_demo_collector_health(env.collector_health_path, today)
    write_demo_collect_log(env.collect_log_path, today)
    rebuild_demo_database(env.db_path, today)
    return env


def write_demo_settings(path: Path, today: date) -> None:
    current_start = today - timedelta(days=31)
    previous_start = current_start - timedelta(days=32)
    previous_end = current_start - timedelta(days=1)
    payload = {
        "state_or_territory": "WA",
        "retailer": "Synergy",
        "retailer_id": "synergy",
        "tariff_setup_mode": "catalog",
        "current_plan": "synergy_ev_add_on",
        "comparison_plan": "previous_flat_or_home_plan",
        "supply_charge_per_day": SUPPLY_CHARGE,
        "import_rates": BUCKET_RATES,
        "export_credit_rate": EXPORT_RATE,
        "comparison_plan_rates": {
            "flat_import_rate": 0.30,
            "supply_charge_per_day": SUPPLY_CHARGE,
            "export_credit_rate": EXPORT_RATE,
        },
        "ev_baseline_watts": 350,
        "ev_data_source": "ocular_export",
        "home_assistant_url": "http://demo-home.local:8123",
        "solar_setup_choice": "manual_later",
        "ev_setup_choice": "ocular_import",
        "battery_setup_choice": "manual_later",
        "billing_period_start_day": current_start.day,
        "current_billing_period_start": current_start.isoformat(),
        "current_billing_period_end": None,
        "previous_billing_period_start": previous_start.isoformat(),
        "previous_billing_period_end": previous_end.isoformat(),
        "billing_cycle_configured": True,
        "petrol_price_per_litre": 1.82,
        "previous_car_l_per_100km": 8.1,
        "ev_kwh_per_100km": 15.5,
        "ev_odometer_km": 24800,
        "car_comparison_enabled": True,
        "recommendation_thresholds": {
            "peak_cost_share_pct": 25,
            "peak_shift_kwh_per_day": 5,
            "solar_self_consumption_pct": 60,
            "ev_detected_min_kwh": 0.5,
            "baseload_excess_pct": 20,
            "high_cost_threshold": 300,
            "positive_peak_share_pct": 20,
            "positive_self_consumption_pct": 70,
        },
    }
    write_json(path, payload)


def write_demo_home_profile(path: Path) -> None:
    payload = {
        "schema_version": 1,
        "household_id": "default_household",
        "location_region": "WA",
        "electricity_retailer": "Synergy",
        "current_tariff": "Synergy EV Add On",
        "solar_installed": True,
        "solar_capacity_kw": 6.6,
        "inverter_brand": "DemoSolar",
        "inverter_model": "DS-5000",
        "battery_installed": True,
        "battery_capacity_kwh": 9.6,
        "ev_owned": True,
        "vehicle_make": "Demo Motors",
        "vehicle_model": "Range 3",
        "charger_type": "7 kW wall charger",
        "household_monitoring_available": True,
        "home_assistant_available": False,
        "demo": True,
    }
    write_json(path, payload)


def write_demo_weather(path: Path, today: date) -> None:
    rows = []
    start = today - timedelta(days=44)
    for index in range(45):
        day = start + timedelta(days=index)
        cloud = 35 + 25 * math.sin(index / 4)
        rows.append(
            {
                "date": day.isoformat(),
                "location": "Perth, WA",
                "condition": "Mostly sunny" if cloud < 45 else "Partly cloudy",
                "min_c": round(8 + 2 * math.sin(index / 7), 1),
                "max_c": round(21 + 4 * math.sin(index / 5), 1),
                "cloud_cover_pct": round(max(10, min(85, cloud)), 1),
                "rain_mm": round(max(0, 3 * math.sin(index / 6) - 1.2), 1),
                "solar_irradiance_index": round(max(0.35, min(0.95, 1 - cloud / 120)), 2),
            }
        )
    write_json(path, {"source": "fictional_demo_weather", "days": rows})


def write_demo_recommendations(path: Path) -> None:
    write_json(
        path,
        {
            "source": "fictional_demo_seed",
            "items": [
                {
                    "title": "Shift flexible evening load out of Peak",
                    "reason": "The demo household uses visible energy during the 3pm-9pm Peak window.",
                    "confidence": "High",
                },
                {
                    "title": "Increase daytime self-consumption",
                    "reason": "Sunny demo days export useful solar while some grid imports occur later.",
                    "confidence": "Medium",
                },
                {
                    "title": "Keep EV charging in the low-cost window",
                    "reason": "Most demo EV charging is already off-peak, with one Peak session to review.",
                    "confidence": "High",
                },
            ],
        },
    )


def write_demo_collector_health(path: Path, today: date) -> None:
    timestamp = datetime.combine(today, time(8, 5), tzinfo=LOCAL_TIMEZONE).isoformat(timespec="seconds")
    write_json(
        path,
        {
            "status": "ok",
            "failure_type": None,
            "message": "Demo data generated locally; no live providers required.",
            "last_attempt_at": timestamp,
            "last_success_at": timestamp,
            "last_failure_at": None,
            "failure_started_at": None,
            "consecutive_failures": 0,
            "notification_sent_at": None,
            "last_measurement_count": 45 * 96,
            "last_successful_report_generation": timestamp,
            "last_report_failure_at": None,
        },
    )


def write_demo_collect_log(path: Path, today: date) -> None:
    timestamp = datetime.combine(today, time(8, 5), tzinfo=LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(f"{timestamp} INFO Collection succeeded for fictional demo data\n", encoding="utf-8")


def rebuild_demo_database(path: Path, today: date) -> None:
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as conn:
        create_readings_table(conn)
        insert_demo_readings(conn, today)
        from engine.data_engine import DataEngine
        from engine.models import TimelineEntry

        engine = DataEngine(connection=conn)
        engine.validate_database()
        insert_demo_ev_sessions(conn, today)
        for bill in demo_bills(today):
            engine.import_synergy_bill(bill, import_job_id="demo")
        engine.persist_timeline_entries(
            [
                TimelineEntry(
                    timestamp=(today - timedelta(days=28)).isoformat(),
                    title="Demo tariff selected",
                    summary="Fictional household moved to an EV time-of-use tariff.",
                    financial_impact="Lower daytime EV charging cost",
                    confidence="High",
                    evidence_sources=["Demo Home Profile", "Demo tariff settings"],
                    event_type="demo_setup",
                ),
                TimelineEntry(
                    timestamp=(today - timedelta(days=10)).isoformat(),
                    title="Peak usage review",
                    summary="Evening cooking and flexible loads created a visible Peak cost cluster.",
                    financial_impact="About $8 this billing period",
                    confidence="Medium",
                    evidence_sources=["Demo readings"],
                    event_type="demo_recommendation",
                ),
                TimelineEntry(
                    timestamp=(today - timedelta(days=3)).isoformat(),
                    title="EV charging imported",
                    summary="Demo Ocular sessions added measured EV charging history.",
                    financial_impact="High-confidence EV charging cost",
                    confidence="High",
                    evidence_sources=["Demo Ocular import"],
                    event_type="demo_import",
                ),
            ]
        )


def create_readings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE readings (
            household_id TEXT DEFAULT 'default_household',
            measurement_time TEXT PRIMARY KEY,
            tariff_bucket TEXT,
            production_w REAL,
            consumption_w REAL,
            solar_to_grid_w REAL,
            grid_to_home_w REAL,
            production_kwh REAL,
            consumption_kwh REAL,
            solar_to_home_kwh REAL,
            solar_to_grid_kwh REAL,
            grid_to_home_kwh REAL,
            import_cost REAL
        )
        """
    )


def insert_demo_readings(conn: sqlite3.Connection, today: date) -> None:
    start = today - timedelta(days=44)
    rows = []
    for day_index in range(45):
        day = start + timedelta(days=day_index)
        weather_factor = 0.72 + 0.18 * math.sin(day_index / 5)
        weekend = day.weekday() >= 5
        for slot in range(96):
            minutes = slot * 15
            hour = minutes / 60
            timestamp = datetime.combine(day, time()) + timedelta(minutes=minutes)
            bucket = tariff_bucket(hour)
            daylight = max(0.0, math.sin((hour - 6) / 12 * math.pi))
            production_kwh = daylight * weather_factor * 0.34
            base = 0.11 + (0.03 if weekend else 0.0)
            morning = 0.13 if 6.5 <= hour <= 8.5 else 0.0
            evening = 0.34 if 17 <= hour <= 21 else 0.0
            pool = 0.18 if 11 <= hour <= 14 and day_index % 2 == 0 else 0.0
            ev = 0.0
            if day_index % 4 == 1 and (23 <= hour or hour < 1):
                ev = 0.55
            if day_index % 17 == 3 and 18 <= hour <= 19.5:
                ev = 0.48
            consumption_kwh = base + morning + evening + pool + ev
            solar_to_home = min(production_kwh, consumption_kwh)
            solar_to_grid = max(production_kwh - consumption_kwh, 0.0)
            grid_to_home = max(consumption_kwh - production_kwh, 0.0)
            rows.append(
                (
                    "default_household",
                    timestamp.replace(tzinfo=LOCAL_TIMEZONE).isoformat(timespec="seconds"),
                    bucket,
                    production_kwh * 4 * 1000,
                    consumption_kwh * 4 * 1000,
                    solar_to_grid * 4 * 1000,
                    grid_to_home * 4 * 1000,
                    production_kwh,
                    consumption_kwh,
                    solar_to_home,
                    solar_to_grid,
                    grid_to_home,
                    grid_to_home * BUCKET_RATES[bucket],
                )
            )
    conn.executemany(
        """
        INSERT INTO readings (
            household_id, measurement_time, tariff_bucket, production_w, consumption_w,
            solar_to_grid_w, grid_to_home_w, production_kwh, consumption_kwh,
            solar_to_home_kwh, solar_to_grid_kwh, grid_to_home_kwh, import_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def insert_demo_ev_sessions(conn: sqlite3.Connection, today: date) -> None:
    session_rows = []
    for index, offset in enumerate((29, 24, 19, 15, 10, 5, 2)):
        start_day = today - timedelta(days=offset)
        if index == 3:
            start_dt = datetime.combine(start_day, time(18, 15), tzinfo=LOCAL_TIMEZONE)
            bucket = "peak"
            energy = 14.2
        else:
            start_dt = datetime.combine(start_day, time(23, 15), tzinfo=LOCAL_TIMEZONE)
            bucket = "overnight"
            energy = 18.0 + (index % 3) * 2.5
        end_dt = start_dt + timedelta(hours=2, minutes=30)
        rate = BUCKET_RATES[bucket]
        cost = energy * rate
        breakdown = {name: {"kwh": 0.0, "cost": 0.0} for name in BUCKET_RATES}
        breakdown[bucket] = {"kwh": energy, "cost": cost}
        session_rows.append(
            (
                "default_household",
                start_dt.isoformat(timespec="seconds"),
                end_dt.isoformat(timespec="seconds"),
                energy,
                "ocular_export",
                cost,
                f"DEMO-EV-{index + 1}",
                end_dt.isoformat(timespec="seconds"),
                7.2,
                "completed",
                json.dumps(breakdown, sort_keys=True),
                150,
                0,
                breakdown["super_off_peak"]["kwh"],
                breakdown["super_off_peak"]["cost"],
                breakdown["overnight"]["kwh"],
                breakdown["overnight"]["cost"],
                breakdown["off_peak"]["kwh"],
                breakdown["off_peak"]["cost"],
                breakdown["peak"]["kwh"],
                breakdown["peak"]["cost"],
            )
        )
    conn.executemany(
        """
        INSERT INTO ev_sessions (
            household_id, start_time, end_time, energy_kwh, source, cost_estimate,
            transaction_id, last_seen, max_power_kw, status, tariff_breakdown_json,
            duration_minutes, cost_partial, super_off_peak_kwh, super_off_peak_cost,
            overnight_kwh, overnight_cost, off_peak_kwh, off_peak_cost, peak_kwh, peak_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        session_rows,
    )


def demo_bills(today: date) -> list[dict]:
    previous_start = today - timedelta(days=63)
    first_end = previous_start + timedelta(days=31)
    second_start = first_end + timedelta(days=1)
    second_end = second_start + timedelta(days=31)
    return [
        demo_bill("DEMO-1001", previous_start, first_end, 176.45, 32),
        demo_bill("DEMO-1002", second_start, second_end, 189.30, 32),
    ]


def demo_bill(invoice: str, start: date, end: date, total: float, days: int) -> dict:
    return {
        "retailer": "Synergy",
        "account_number": "DEMO-ACCOUNT-123",
        "invoice_number": invoice,
        "issue_date": (end + timedelta(days=3)).isoformat(),
        "due_date": (end + timedelta(days=17)).isoformat(),
        "bill_total": total,
        "total_amount_owing": total,
        "opening_balance": 0.0,
        "supply_address": "10 Demo Way, Perth WA",
        "nmi": "DEMO-NMI-000",
        "next_scheduled_read_date": (end + timedelta(days=30)).isoformat(),
        "meter_number": "DEMO-METER",
        "periods": [
            {
                "plan_name": "Synergy EV Add On",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            }
        ],
        "line_items": [
            {
                "category": "supply",
                "description": "Supply charge",
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "days": days,
                "unit_rate": SUPPLY_CHARGE,
                "amount": days * SUPPLY_CHARGE,
            },
            {
                "category": "usage",
                "bucket": "peak",
                "description": "Peak Charges",
                "kwh": 86.2,
                "unit_rate": BUCKET_RATES["peak"],
                "amount": 86.2 * BUCKET_RATES["peak"],
            },
            {
                "category": "usage",
                "bucket": "super_off_peak",
                "description": "Super Off Peak Charge",
                "kwh": 142.4,
                "unit_rate": BUCKET_RATES["super_off_peak"],
                "amount": 142.4 * BUCKET_RATES["super_off_peak"],
            },
            {
                "category": "export",
                "bucket": "off_peak_export",
                "description": "Distributed Energy Buyback Off Peak",
                "kwh": 108.0,
                "unit_rate": EXPORT_RATE,
                "amount": -108.0 * EXPORT_RATE,
            },
        ],
        "gst": round(total / 11, 2),
        "warnings": [],
        "confidence": "high",
        "totals": {"bill_total": total},
    }


def tariff_bucket(hour: float) -> str:
    if 9 <= hour < 15:
        return "super_off_peak"
    if hour >= 23 or hour < 6:
        return "overnight"
    if 15 <= hour < 21:
        return "peak"
    return "off_peak"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
