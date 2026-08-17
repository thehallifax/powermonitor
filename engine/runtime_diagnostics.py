#!/usr/bin/env python3
"""Runtime environment diagnostics for local PowerMonitor installs."""

import importlib.util
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def runtime_diagnostics(
    *,
    project_root: Path,
    database_path: Path,
    report_path: Optional[Path] = None,
    settings_path: Optional[Path] = None,
    active_tariff_version: Optional[dict[str, Any]] = None,
    health_summary: Optional[dict[str, Any]] = None,
    web_host: Optional[str] = None,
    web_port: Optional[int] = None,
    working_directory: Optional[Path] = None,
    launch_agents_dir: Optional[Path] = None,
) -> dict[str, Any]:
    executable_path = Path(sys.executable)
    executable = executable_path.resolve()
    report_timestamp = _file_timestamp(report_path) if report_path else None
    venv_path = project_root / ".venv"
    in_venv = bool(getattr(sys, "base_prefix", sys.prefix) != sys.prefix)
    launch_evidence = _launch_agent_python_evidence(project_root, venv_path, launch_agents_dir=launch_agents_dir)
    prefix_in_project_venv = _is_relative_to(Path(sys.prefix), venv_path)
    in_project_venv = (
        _is_relative_to(executable_path, venv_path)
        or _is_relative_to(executable, venv_path)
        or prefix_in_project_venv
        or bool(launch_evidence.get("uses_project_venv"))
    )
    playwright_available = _module_available("playwright")
    pypdf_available = _module_available("pypdf")
    chromium = _playwright_chromium_status(playwright_available)
    pdftotext_path = shutil.which("pdftotext")

    warnings = []
    if not in_project_venv:
        warnings.append("PowerMonitor is not running from the project .venv Python.")
    if not playwright_available:
        warnings.append("Automatic SolarEdge sign-in is unavailable because Playwright is not installed in the active Python environment.")
    elif not chromium.get("available"):
        warnings.append("Automatic SolarEdge sign-in is unavailable because Playwright Chromium is not installed for the active Python environment.")
    if not pypdf_available:
        warnings.append("Synergy PDF import is unavailable because pypdf is not installed.")

    return {
        "python_version": sys.version.split()[0],
        "python_version_full": sys.version,
        "executable": str(executable),
        "launch_executable": launch_evidence.get("launch_executable"),
        "launch_executables": launch_evidence.get("launch_executables", []),
        "resolved_interpreter": str(executable),
        "prefix": sys.prefix,
        "base_prefix": getattr(sys, "base_prefix", sys.prefix),
        "running_in_venv": in_venv,
        "running_in_project_venv": in_project_venv,
        "launch_agent_uses_project_venv": bool(launch_evidence.get("uses_project_venv")),
        "project_venv": str(venv_path),
        "playwright_available": playwright_available,
        "playwright_chromium_available": bool(chromium.get("available")),
        "playwright_chromium_path": chromium.get("path"),
        "playwright_chromium_error": chromium.get("error"),
        "pypdf_available": pypdf_available,
        "pdftotext_available": bool(pdftotext_path),
        "pdftotext_path": pdftotext_path,
        "project_root": str(project_root),
        "working_directory": str((working_directory or Path.cwd()).resolve()),
        "database_path": str(database_path),
        "settings_path": str(settings_path) if settings_path else None,
        "report_path": str(report_path) if report_path else None,
        "report_generation_timestamp": report_timestamp,
        "web_host": web_host,
        "web_port": web_port,
        "web_bind_address": f"{web_host or 'unknown'}:{web_port or 'unknown'}",
        "git_commit": _git_commit(project_root),
        "active_launch_agent_labels": _active_launch_agent_labels(),
        "active_tariff_version": active_tariff_version,
        "runtime_health_summary": health_summary or {},
        "warnings": warnings,
    }


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _playwright_chromium_status(playwright_available: bool) -> dict[str, Any]:
    if not playwright_available:
        return {"available": False, "path": None, "error": "playwright not installed"}
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        with sync_playwright() as playwright:
            path = Path(playwright.chromium.executable_path)
            return {"available": path.exists(), "path": str(path), "error": None if path.exists() else "chromium executable not found"}
    except Exception as exc:
        return {"available": False, "path": None, "error": exc.__class__.__name__}


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.absolute().relative_to(parent.absolute())
        return True
    except ValueError:
        pass
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _launch_agent_python_evidence(
    project_root: Path,
    venv_path: Path,
    *,
    launch_agents_dir: Optional[Path] = None,
) -> dict[str, Any]:
    launch_agents_dir = launch_agents_dir or Path.home() / "Library" / "LaunchAgents"
    expected = {
        "com.local.powermonitor.collector": project_root / "collector" / "collect_today.py",
        "com.local.powermonitor.web": project_root / "collector" / "web.py",
    }
    launch_executables: list[str] = []
    uses_project_venv = False
    for label, script_path in expected.items():
        plist_path = launch_agents_dir / f"{label}.plist"
        if not plist_path.exists():
            continue
        try:
            with plist_path.open("rb") as handle:
                plist = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            continue
        args = [str(value) for value in plist.get("ProgramArguments", [])]
        if not args or str(script_path) not in args:
            continue
        launch_executable = Path(args[0])
        launch_executables.append(str(launch_executable))
        if _is_relative_to(launch_executable, venv_path):
            uses_project_venv = True
    return {
        "uses_project_venv": uses_project_venv,
        "launch_executable": launch_executables[0] if launch_executables else None,
        "launch_executables": launch_executables,
    }


def _file_timestamp(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return None


def _git_commit(project_root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    commit = result.stdout.strip()
    return commit or None


def _active_launch_agent_labels() -> list[str]:
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    labels = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts:
            label = parts[-1]
            if label.startswith("com.local.powermonitor."):
                labels.append(label)
    return sorted(set(labels))
