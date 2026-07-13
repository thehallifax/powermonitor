#!/usr/bin/env python3
"""Run a SolarEdge historical interval backfill job."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "collector") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "collector"))

from collect_today import (  # noqa: E402
    LOG_PATH,
    OUTFILE,
    SessionRefreshFailure,
    AuthenticationFailure,
    fetch_with_session_recovery,
    generate_report,
    parse_import_summary,
    payload_diagnostics,
    redact_sensitive,
    setup_logging,
)
from engine.data_engine import DataEngine  # noqa: E402
from engine.historical_backfill import (  # noqa: E402
    SOLAREDGE_DATA_TYPES,
    build_backfill_preview,
    job_id_for,
    resolve_backfill_range,
)

DB_PATH = PROJECT_ROOT / "data" / "power.sqlite"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill SolarEdge historical interval readings.")
    parser.add_argument("--range", default="previous_summer", dest="range_preset")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--site-id", default=os.environ.get("POWERMONITOR_SOLAREDGE_SITE_ID", "default"))
    parser.add_argument("--job-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--cancel", action="store_true")
    return parser.parse_args()


def import_day(json_path: Path, job_id: str) -> tuple[int, dict[str, int], str]:
    env = dict(os.environ)
    env["POWERMONITOR_IMPORT_JOB_ID"] = job_id
    imported = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "collector" / "import_solaredge.py"), str(json_path)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    message = imported.stdout.strip()
    match = re.search(r"Imported\s+(\d+)\s+readings", message)
    imported_count = int(match.group(1)) if match else 0
    return imported_count, parse_import_summary(message), message


def run() -> int:
    setup_logging()
    args = parse_args()
    engine = DataEngine(db_path=DB_PATH)

    if args.cancel:
        if not args.job_id:
            raise ValueError("--cancel requires --job-id")
        job = engine.request_historical_backfill_cancel(args.job_id)
        print(f"Cancel requested for historical data job {job['job_id']}")
        return 0

    if args.resume:
        if not args.job_id:
            raise ValueError("--resume requires --job-id")
        job = engine.get_historical_backfill_job(args.job_id)
        if not job:
            raise RuntimeError("Historical data job not found")
        selected_start = date.fromisoformat(str(job["start_date"]))
        selected_end = date.fromisoformat(str(job["end_date"]))
        job_id = str(job["job_id"])
        range_preset = str(job["range_preset"])
        warnings = list(job.get("warnings") or [])
        existing_progress = job.get("progress") or {}
        completed_days = list(existing_progress.get("completed_days") or [])
        imported_or_updated = int(existing_progress.get("imported_or_updated") or 0)
        data_received = int(existing_progress.get("data_received") or 0)
        duplicates = int(existing_progress.get("duplicates_skipped") or 0)
        zero_rows = int(existing_progress.get("zero_payload_rows_skipped") or 0)
    else:
        coverage = engine.get_historical_coverage()
        selected = resolve_backfill_range(args.range_preset, start=args.start, end=args.end)
        preview = build_backfill_preview(
            coverage,
            provider_id="solaredge",
            preset=args.range_preset,
            start=args.start,
            end=args.end,
        )
        job_id = args.job_id or job_id_for("solaredge", args.site_id, selected)
        selected_start = selected.start
        selected_end = selected.end
        range_preset = selected.preset
        warnings = list(preview.get("warnings") or [])
        completed_days = []
        imported_or_updated = 0
        data_received = 0
        duplicates = 0
        zero_rows = 0
        engine.create_historical_backfill_job(
            job_id=job_id,
            provider_id="solaredge",
            site_id=args.site_id,
            range_preset=range_preset,
            start_date=selected_start,
            end_date=selected_end,
            resolution="interval_15m",
            quality_tier="interval_grade",
            data_types=list(SOLAREDGE_DATA_TYPES),
            progress={"chunks_total": (selected_end - selected_start).days + 1, "chunks_completed": 0},
            diagnostics={"preview": preview},
            warnings=warnings,
            status="queued",
        )

    total_days = (selected_end - selected_start).days + 1
    failed_days: list[str] = []

    engine.update_historical_backfill_job(
        job_id,
        status="running",
        progress={
            "chunks_total": total_days,
            "chunks_completed": len(completed_days),
            "current_day": None,
            "completed_days": completed_days,
            "failed_days": failed_days,
            "imported_or_updated": imported_or_updated,
            "data_received": data_received,
            "duplicates_skipped": duplicates,
            "zero_payload_rows_skipped": zero_rows,
        },
        warnings=warnings,
    )

    OUTFILE.parent.mkdir(parents=True, exist_ok=True)
    completed_day_set = set(completed_days)
    for offset in range(total_days):
        current_day = selected_start + timedelta(days=offset)
        if current_day.isoformat() in completed_day_set:
            continue
        current_job = engine.get_historical_backfill_job(job_id) or {}
        if current_job.get("cancel_requested"):
            engine.update_historical_backfill_job(
                job_id,
                status="cancelled",
                progress={
                    "chunks_total": total_days,
                    "chunks_completed": len(completed_days) + len(failed_days),
                    "completed_days": completed_days,
                    "failed_days": failed_days,
                    "imported_or_updated": imported_or_updated,
                    "data_received": data_received,
                },
                warnings=warnings,
                completed=True,
            )
            print(f"Historical backfill cancelled: {job_id}")
            return 2

        temp_outfile = OUTFILE.with_name(f"backfill-solaredge-{current_day.isoformat()}.json.tmp")
        temp_outfile.unlink(missing_ok=True)
        day_success = False
        last_error = None
        for _attempt in range(2):
            try:
                _status, measurement_count, _auth_path = fetch_with_session_recovery(current_day, temp_outfile)
                payload_summary = payload_diagnostics(temp_outfile, current_day)
                payload_measurements = int(payload_summary.get("measurement_count") or measurement_count or 0)
                if payload_measurements <= 0:
                    raise RuntimeError("SolarEdge returned no historical interval readings for this day.")
                imported_count, summary, importer_message = import_day(temp_outfile, job_id)
                imported_or_updated += summary.get("imported_or_updated") or imported_count
                data_received += summary.get("data_received") or payload_measurements
                duplicates += summary.get("duplicates_skipped", 0)
                zero_rows += summary.get("zero_payload_skipped", 0)
                temp_outfile.replace(OUTFILE)
                completed_days.append(current_day.isoformat())
                day_success = True
                if importer_message:
                    print(importer_message)
                print(
                    "Backfilled "
                    f"{current_day.isoformat()}: {payload_measurements} readings checked"
                )
                break
            except (AuthenticationFailure, SessionRefreshFailure) as exc:
                last_error = redact_sensitive(str(exc))
                temp_outfile.unlink(missing_ok=True)
                break
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                last_error = redact_sensitive(str(exc))
                temp_outfile.unlink(missing_ok=True)
        if not day_success:
            failed_days.append(current_day.isoformat())
            warnings.append(f"{current_day.isoformat()}: {last_error or 'No usable interval data returned'}")

        engine.update_historical_backfill_job(
            job_id,
            status="running",
            progress={
                "chunks_total": total_days,
                "chunks_completed": len(completed_days) + len(failed_days),
                "current_day": current_day.isoformat(),
                "completed_days": completed_days,
                "failed_days": failed_days,
                "imported_or_updated": imported_or_updated,
                "data_received": data_received,
                "duplicates_skipped": duplicates,
                "zero_payload_rows_skipped": zero_rows,
            },
            warnings=warnings[-50:],
        )

    status = "complete" if completed_days else "failed"
    diagnostics = {
        "days_requested": total_days,
        "days_imported": len(completed_days),
        "days_failed": len(failed_days),
        "range_preset": range_preset,
        "report_regenerated": False,
    }
    if completed_days:
        generate_report()
        diagnostics["report_regenerated"] = True
    engine.update_historical_backfill_job(
        job_id,
        status=status,
        progress={
            "chunks_total": total_days,
            "chunks_completed": total_days,
            "completed_days": completed_days,
            "failed_days": failed_days,
            "imported_or_updated": imported_or_updated,
            "data_received": data_received,
            "duplicates_skipped": duplicates,
            "zero_payload_rows_skipped": zero_rows,
        },
        diagnostics=diagnostics,
        warnings=warnings[-50:],
        completed=True,
    )
    print(
        f"Historical backfill {status}: {len(completed_days)}/{total_days} days imported; "
        f"{imported_or_updated} readings imported or updated; see {LOG_PATH}"
    )
    return 0 if completed_days else 1


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as exc:
        print(f"Historical backfill failed: {redact_sensitive(str(exc))}", file=sys.stderr)
        raise
