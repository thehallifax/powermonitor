#!/usr/bin/env python3
"""Validate PowerMonitor macOS LaunchAgent installation."""

from __future__ import annotations

import json
import plistlib
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
SUPPORTED = {
    "collector": {
        "label": "com.local.powermonitor.collector",
        "script": PROJECT_ROOT / "collector" / "collect_today.py",
    },
    "web": {
        "label": "com.local.powermonitor.web",
        "script": PROJECT_ROOT / "collector" / "web.py",
    },
}
OBSOLETE_PREFIXES = ("com.penrite.powermonitor",)
RETIRED_LABELS = ("com.local.powermonitor.ocpp",)


@dataclass
class PlistInfo:
    path: Path
    label: str
    working_directory: Optional[str]
    program_arguments: list[str]
    role: str
    obsolete: bool


def role_for_label(label: str, program_arguments: Iterable[str] = ()) -> str:
    for role, expected in SUPPORTED.items():
        if label == expected["label"]:
            return role
    if label == "com.penrite.powermonitor":
        return "collector"
    suffix = label.rsplit(".", 1)[-1]
    if suffix in SUPPORTED:
        return suffix
    arguments = " ".join(program_arguments)
    if "collect_today.py" in arguments:
        return "collector"
    if "web.py" in arguments:
        return "web"
    if "import_ha_ocpp.py" in arguments:
        return "retired_ocpp"
    return "unknown"


def is_obsolete(label: str, path: Path) -> bool:
    return any(label.startswith(prefix) or path.stem.startswith(prefix) for prefix in OBSOLETE_PREFIXES)


def plist_paths(launch_agents_dir: Path = LAUNCH_AGENTS_DIR) -> list[Path]:
    patterns = [f"{data['label']}.plist" for data in SUPPORTED.values()]
    patterns.extend(f"{label}.plist" for label in RETIRED_LABELS)
    for prefix in OBSOLETE_PREFIXES:
        patterns.append(f"{prefix}*.plist")
    paths: set[Path] = set()
    for pattern in patterns:
        paths.update(launch_agents_dir.glob(pattern))
    return sorted(paths)


def read_plists(launch_agents_dir: Path = LAUNCH_AGENTS_DIR) -> list[PlistInfo]:
    records: list[PlistInfo] = []
    for path in plist_paths(launch_agents_dir):
        try:
            with path.open("rb") as handle:
                plist = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            records.append(PlistInfo(path, path.stem, None, [], "unknown", True))
            continue
        args = [str(value) for value in plist.get("ProgramArguments", [])]
        label = str(plist.get("Label") or path.stem)
        records.append(
            PlistInfo(
                path=path,
                label=label,
                working_directory=plist.get("WorkingDirectory"),
                program_arguments=args,
                role=role_for_label(label, args),
                obsolete=is_obsolete(label, path),
            )
        )
    return records


def launchctl_labels() -> list[str]:
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False)
    labels: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        label = parts[-1]
        if "powermonitor" in label and (label.startswith("com.local.") or label.startswith("com.penrite.")):
            labels.append(label)
    return sorted(labels)


def validate(launch_agents_dir: Path = LAUNCH_AGENTS_DIR, loaded_labels: Optional[list[str]] = None) -> dict:
    plists = read_plists(launch_agents_dir)
    loaded_labels = launchctl_labels() if loaded_labels is None else sorted(loaded_labels)
    issues: list[str] = []
    expected_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not expected_python.exists():
        issues.append(f"missing project virtualenv Python: {expected_python}")

    plists_by_role: dict[str, list[PlistInfo]] = defaultdict(list)
    for record in plists:
        plists_by_role[record.role].append(record)

    loaded_by_role: dict[str, list[str]] = defaultdict(list)
    for label in loaded_labels:
        loaded_by_role[role_for_label(label)].append(label)

    for role, expected in SUPPORTED.items():
        expected_label = expected["label"]
        role_plists = plists_by_role.get(role, [])
        supported_plists = [item for item in role_plists if item.label == expected_label and not item.obsolete]
        if not supported_plists:
            issues.append(f"missing LaunchAgent plist for {role}: {expected_label}")
        if len(role_plists) > 1:
            issues.append(f"duplicate LaunchAgent plists for {role}: {', '.join(item.path.name for item in role_plists)}")
        role_loaded = loaded_by_role.get(role, [])
        if not role_loaded:
            issues.append(f"missing loaded service for {role}: {expected_label}")
        if len(role_loaded) > 1:
            issues.append(f"duplicate loaded services for {role}: {', '.join(role_loaded)}")

    for record in plists:
        expected = SUPPORTED.get(record.role)
        if record.obsolete:
            issues.append(f"obsolete namespace plist present: {record.path.name}")
            continue
        if record.label != record.path.stem:
            issues.append(f"mismatched plist label/file: {record.path.name} contains {record.label}")
        if expected and record.label != expected["label"]:
            issues.append(f"unexpected label for {record.role}: {record.label}")
        if expected and str(expected["script"]) not in record.program_arguments:
            issues.append(f"mismatched ProgramArguments for {record.role}: expected {expected['script']}")
        if expected and (not record.program_arguments or Path(record.program_arguments[0]).absolute() != expected_python.absolute()):
            actual = record.program_arguments[0] if record.program_arguments else "missing"
            issues.append(f"incorrect Python executable for {record.role}: {actual}; expected {expected_python}")
        if record.working_directory != str(PROJECT_ROOT):
            issues.append(
                f"incorrect working directory for {record.label}: "
                f"{record.working_directory or 'missing'}"
            )

    for label in loaded_labels:
        if any(label.startswith(prefix) for prefix in OBSOLETE_PREFIXES):
            issues.append(f"obsolete namespace service loaded: {label}")
        if label in RETIRED_LABELS:
            issues.append(f"retired OCPP service loaded: {label}")

    for record in plists:
        if record.label in RETIRED_LABELS:
            issues.append(f"retired OCPP plist present: {record.path.name}")

    return {
        "ok": not issues,
        "project_root": str(PROJECT_ROOT),
        "launch_agents_dir": str(launch_agents_dir),
        "expected_labels": {role: data["label"] for role, data in SUPPORTED.items()},
        "plist_files": [
            {
                "path": str(record.path),
                "label": record.label,
                "role": record.role,
                "working_directory": record.working_directory,
                "obsolete": record.obsolete,
            }
            for record in plists
        ],
        "loaded_services": loaded_labels,
        "issues": sorted(set(issues)),
    }


def main() -> int:
    result = validate()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
