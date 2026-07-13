#!/usr/bin/env python3
"""PowerMonitor runtime and canonical-path diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report
import validate_launch_agents

SUPPORTED_LABELS = {
    "collector": "com.local.powermonitor.collector",
    "web": "com.local.powermonitor.web",
    "ocpp": "com.local.powermonitor.ocpp",
}
DEFAULT_STALE_REPORT_MINUTES = 20.0


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_report_path() -> Path:
    return report.REPORT_PATH.resolve()


def file_hash(path: Path) -> Optional[str]:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def live_dashboard_url(host: str, port: int) -> str:
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    return f"http://{connect_host}:{port}/"


def live_hash_matches_report(
    *,
    report_path: Path,
    host: str,
    port: int,
    fetcher: Optional[Callable[[str], bytes]] = None,
) -> dict[str, Any]:
    url = live_dashboard_url(host, port)
    report_digest = file_hash(report_path)
    fetch = fetcher or fetch_url
    try:
        live_payload = fetch(url)
        live_digest = sha256_bytes(live_payload)
        error = None
    except Exception as exc:
        live_digest = None
        error = exc.__class__.__name__
    return {
        "url": url,
        "report_hash": report_digest,
        "live_hash": live_digest,
        "matches": bool(report_digest and live_digest and report_digest == live_digest),
        "error": error,
    }


def fetch_url(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read()


def report_age_minutes(report_path: Path, now: Optional[float] = None) -> Optional[float]:
    try:
        modified = report_path.stat().st_mtime
    except OSError:
        return None
    return max(0.0, ((time.time() if now is None else now) - modified) / 60.0)


def stale_report(report_path: Path, threshold_minutes: float = DEFAULT_STALE_REPORT_MINUTES) -> dict[str, Any]:
    age = report_age_minutes(report_path)
    return {
        "age_minutes": age,
        "threshold_minutes": threshold_minutes,
        "stale": age is None or age > threshold_minutes,
    }


def loaded_powermonitor_launch_agents() -> list[str]:
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False)
    labels = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts:
            label = parts[-1]
            if label.startswith("com.local.powermonitor."):
                labels.append(label)
    return sorted(labels)


def duplicate_processes(process_rows: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    rows = process_rows if process_rows is not None else running_powermonitor_processes()
    by_role: dict[str, list[dict[str, Any]]] = {role: [] for role in SUPPORTED_LABELS}
    for row in rows:
        role = role_from_command(str(row.get("command") or ""))
        if role:
            by_role[role].append(row)
    duplicates = {role: items for role, items in by_role.items() if len(items) > 1}
    return {
        "processes": rows,
        "duplicates": duplicates,
        "duplicate_roles": sorted(duplicates),
    }


def running_powermonitor_processes() -> list[dict[str, Any]]:
    result = subprocess.run(["ps", "ax", "-o", "pid=", "-o", "command="], capture_output=True, text=True, check=False)
    rows = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        if "PowerMonitor" not in command and "/collector/" not in command:
            continue
        role = role_from_command(command)
        if role:
            rows.append({"pid": int(pid_text), "role": role, "command": command})
    return rows


def role_from_command(command: str) -> Optional[str]:
    if "collect_today.py" in command:
        return "collector"
    if "web.py" in command:
        return "web"
    if "import_ha_ocpp.py" in command:
        return "ocpp"
    return None


def listener_rows(port: int = 8765) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    rows = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 9:
            rows.append({"command": parts[0], "pid": parts[1], "address": parts[-2], "state": parts[-1]})
    return rows


def duplicate_listeners(port: int = 8765, rows: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    listeners = rows if rows is not None else listener_rows(port)
    return {
        "port": port,
        "listeners": listeners,
        "duplicate": len(listeners) > 1,
        "count": len(listeners),
    }


def plist_runtime_mismatches(
    *,
    launch_agents_dir: Path = validate_launch_agents.LAUNCH_AGENTS_DIR,
    expected_working_directory: Path = PROJECT_ROOT,
    expected_executable: Optional[Path] = None,
) -> dict[str, Any]:
    expected_executable = (expected_executable or Path(sys.executable)).resolve()
    wrong_working_directory = []
    mismatched_executable = []
    for role, label in SUPPORTED_LABELS.items():
        path = launch_agents_dir / f"{label}.plist"
        if not path.exists():
            continue
        try:
            with path.open("rb") as handle:
                plist = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            continue
        working_directory = plist.get("WorkingDirectory")
        args = [str(item) for item in plist.get("ProgramArguments", [])]
        executable = Path(args[0]).resolve() if args else None
        if working_directory != str(expected_working_directory):
            wrong_working_directory.append({"label": label, "working_directory": working_directory})
        if executable != expected_executable:
            mismatched_executable.append(
                {
                    "label": label,
                    "executable": str(executable) if executable else None,
                    "expected_executable": str(expected_executable),
                }
            )
    return {
        "wrong_working_directory": wrong_working_directory,
        "mismatched_executable": mismatched_executable,
    }


def missing_files(report_path: Path) -> list[str]:
    checks = {
        "config": report.SETTINGS_PATH,
        "database": report.DB_PATH,
        "report": report_path,
    }
    return [name for name, path in checks.items() if not path.exists()]


def resolve_web_bind() -> tuple[str, int]:
    try:
        return report.web_bind_for_runtime_check()
    except Exception:
        return "127.0.0.1", 8765


def diagnostics_payload(
    *,
    stale_minutes: float = DEFAULT_STALE_REPORT_MINUTES,
    fetcher: Optional[Callable[[str], bytes]] = None,
) -> dict[str, Any]:
    report_path = canonical_report_path()
    host, port = resolve_web_bind()
    launch_agents = validate_launch_agents.validate()
    plist_mismatches = plist_runtime_mismatches(expected_executable=Path(sys.executable))
    process_duplicates = duplicate_processes()
    listener_duplicates = duplicate_listeners(port)
    stale = stale_report(report_path, stale_minutes)
    missing = missing_files(report_path)
    issues = []
    issues.extend(launch_agents.get("issues") or [])
    if process_duplicates["duplicate_roles"]:
        issues.append("duplicate processes: " + ", ".join(process_duplicates["duplicate_roles"]))
    if listener_duplicates["duplicate"]:
        issues.append(f"duplicate listeners on port {port}")
    if stale["stale"]:
        issues.append("stale report")
    if plist_mismatches["wrong_working_directory"]:
        issues.append("wrong working directory")
    if plist_mismatches["mismatched_executable"]:
        issues.append("mismatched executable")
    if missing:
        issues.append("missing files: " + ", ".join(missing))
    live_match = live_hash_matches_report(report_path=report_path, host=host, port=port, fetcher=fetcher)
    if not live_match["matches"]:
        issues.append("live dashboard does not match report hash")
    return {
        "ok": not issues,
        "canonical_report_path": str(report_path),
        "live_report_hash": live_match,
        "loaded_launch_agents": launch_agents.get("loaded_services", loaded_powermonitor_launch_agents()),
        "launch_agent_validation": launch_agents,
        "duplicate_processes": process_duplicates,
        "duplicate_listeners": listener_duplicates,
        "stale_report": stale,
        "wrong_working_directory": plist_mismatches["wrong_working_directory"],
        "mismatched_executable": plist_mismatches["mismatched_executable"],
        "missing_files": missing,
        "issues": sorted(set(issues)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Report PowerMonitor runtime diagnostics.")
    parser.add_argument("--stale-minutes", type=float, default=DEFAULT_STALE_REPORT_MINUTES)
    args = parser.parse_args()
    payload = diagnostics_payload(stale_minutes=args.stale_minutes)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
