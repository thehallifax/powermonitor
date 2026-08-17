#!/usr/bin/env python3
"""Generate macOS LaunchAgents for PowerMonitor using the project Python."""

import argparse
import plistlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
SERVICE_LABELS = {
    "collector": "com.local.powermonitor.collector",
    "web": "com.local.powermonitor.web",
}
OBSOLETE_LABEL_PREFIXES = ("com.penrite.powermonitor",)
RETIRED_LABELS = ("com.local.powermonitor.ocpp",)


def discover_python(project_root: Path = PROJECT_ROOT) -> Path:
    venv_python = project_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        raise FileNotFoundError(f"Project virtualenv Python not found: {venv_python}")
    return venv_python.absolute()


def launch_agent_definitions(project_root: Path, python_executable: Path) -> dict[str, dict]:
    logs = project_root / "logs"
    definitions = {
        "com.local.powermonitor.collector": {
            "Label": "com.local.powermonitor.collector",
            "ProgramArguments": [str(python_executable), str(project_root / "collector" / "collect_today.py")],
            "WorkingDirectory": str(project_root),
            "StartInterval": 900,
            "RunAtLoad": True,
            "StandardOutPath": str(logs / "launchagent.collector.out.log"),
            "StandardErrorPath": str(logs / "launchagent.collector.err.log"),
        },
        "com.local.powermonitor.web": {
            "Label": "com.local.powermonitor.web",
            "ProgramArguments": [str(python_executable), str(project_root / "collector" / "web.py")],
            "WorkingDirectory": str(project_root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "launchagent.web.out.log"),
            "StandardErrorPath": str(logs / "launchagent.web.err.log"),
        },
    }
    return definitions


def obsolete_launch_agent_paths(launch_agents_dir: Path = LAUNCH_AGENTS_DIR) -> list[Path]:
    paths: list[Path] = []
    for prefix in OBSOLETE_LABEL_PREFIXES:
        paths.extend(sorted(launch_agents_dir.glob(f"{prefix}*.plist")))
    paths.extend(launch_agents_dir / f"{label}.plist" for label in RETIRED_LABELS)
    return sorted(set(paths))


def obsolete_labels(launch_agents_dir: Path = LAUNCH_AGENTS_DIR) -> list[str]:
    labels = set()
    for path in obsolete_launch_agent_paths(launch_agents_dir):
        try:
            with path.open("rb") as handle:
                plist = plistlib.load(handle)
            labels.add(str(plist.get("Label") or path.stem))
        except (OSError, plistlib.InvalidFileException):
            labels.add(path.stem)
    for prefix in OBSOLETE_LABEL_PREFIXES:
        labels.add(prefix)
        labels.update(f"{prefix}.{role}" for role in SERVICE_LABELS)
    labels.update(RETIRED_LABELS)
    return sorted(labels)


def remove_obsolete_launch_agents(launch_agents_dir: Path = LAUNCH_AGENTS_DIR) -> list[str]:
    """Unload and remove obsolete namespace and retired OCPP LaunchAgents.

    Cleanup is best-effort so fresh installs and repeated migrations remain
    idempotent.
    """
    messages: list[str] = []
    gui_domain = f"gui/{os.getuid()}"
    for label in obsolete_labels(launch_agents_dir):
        result = subprocess.run(
            ["launchctl", "bootout", f"{gui_domain}/{label}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            messages.append(f"Unloaded obsolete LaunchAgent {label}")
    for path in obsolete_launch_agent_paths(launch_agents_dir):
        try:
            path.unlink()
            messages.append(f"Removed obsolete plist {path}")
        except FileNotFoundError:
            continue
    return messages


def write_launch_agents(
    *,
    project_root: Path = PROJECT_ROOT,
    python_executable: Optional[Path] = None,
    launch_agents_dir: Path = LAUNCH_AGENTS_DIR,
    selected: Iterable[str] = ("collector", "web"),
) -> list[Path]:
    python_path = (python_executable or discover_python(project_root)).absolute()
    expected_python = (project_root / ".venv" / "bin" / "python").absolute()
    if python_path != expected_python:
        raise ValueError(f"LaunchAgents must use the project virtualenv Python: {expected_python}")
    wanted = set(selected)
    definitions = launch_agent_definitions(project_root.resolve(), python_path)
    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    (project_root / "logs").mkdir(parents=True, exist_ok=True)
    written = []
    for label, plist in definitions.items():
        role = label.rsplit(".", 1)[-1]
        if role not in wanted:
            continue
        path = launch_agents_dir / f"{label}.plist"
        with path.open("wb") as handle:
            plistlib.dump(plist, handle, sort_keys=True)
        written.append(path)
    return written


def reload_commands(paths: Iterable[Path]) -> list[str]:
    commands = []
    for path in paths:
        label = path.stem
        commands.append(f"launchctl bootout gui/$(id -u)/{label} 2>/dev/null || true")
        commands.append(f"launchctl bootout gui/$(id -u) {path} 2>/dev/null || true")
        commands.append(f"launchctl bootstrap gui/$(id -u) {path}")
    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate PowerMonitor LaunchAgents with the active project Python.")
    parser.add_argument("--python", dest="python_path", type=Path, default=None, help="Python executable to use. Defaults to .venv/bin/python when present.")
    parser.add_argument("--launch-agents-dir", type=Path, default=LAUNCH_AGENTS_DIR)
    parser.add_argument("--only", choices=["collector", "web"], action="append", help="Generate only the named LaunchAgent. Can be repeated.")
    parser.add_argument("--keep-obsolete", action="store_true", help="Do not remove obsolete com.penrite.powermonitor LaunchAgents.")
    args = parser.parse_args()

    selected = tuple(args.only) if args.only else ("collector", "web")
    expected_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    python_path = (args.python_path or discover_python()).absolute()
    if python_path != expected_python.absolute():
        raise SystemExit(f"LaunchAgents must use the project virtualenv Python: {expected_python}")
    if not python_path.exists():
        raise SystemExit(f"Python executable not found: {python_path}")
    if not args.keep_obsolete:
        for message in remove_obsolete_launch_agents(args.launch_agents_dir):
            print(message)
    written = write_launch_agents(
        python_executable=python_path,
        launch_agents_dir=args.launch_agents_dir,
        selected=selected,
    )
    print(f"Using Python: {python_path}")
    for path in written:
        print(f"Wrote {path}")
    print("\nReload with:")
    for command in reload_commands(written):
        print(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
