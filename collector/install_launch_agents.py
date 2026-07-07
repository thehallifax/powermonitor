#!/usr/bin/env python3
"""Generate macOS LaunchAgents for PowerMonitor using the project Python."""

import argparse
import plistlib
import sys
from pathlib import Path
from typing import Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def discover_python(project_root: Path = PROJECT_ROOT) -> Path:
    venv_python = project_root / ".venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python.resolve()
    return Path(sys.executable).resolve()


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
    ocpp_importer = project_root / "collector" / "import_ha_ocpp.py"
    if ocpp_importer.exists():
        definitions["com.local.powermonitor.ocpp"] = {
            "Label": "com.local.powermonitor.ocpp",
            "ProgramArguments": [str(python_executable), str(ocpp_importer)],
            "WorkingDirectory": str(project_root),
            "StartInterval": 300,
            "RunAtLoad": False,
            "StandardOutPath": str(logs / "launchagent.ocpp.out.log"),
            "StandardErrorPath": str(logs / "launchagent.ocpp.err.log"),
        }
    return definitions


def write_launch_agents(
    *,
    project_root: Path = PROJECT_ROOT,
    python_executable: Optional[Path] = None,
    launch_agents_dir: Path = LAUNCH_AGENTS_DIR,
    selected: Iterable[str] = ("collector", "web", "ocpp"),
) -> list[Path]:
    python_path = (python_executable or discover_python(project_root)).resolve()
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
        commands.append(f"launchctl bootout gui/$(id -u) {path} 2>/dev/null || true")
        commands.append(f"launchctl bootstrap gui/$(id -u) {path}")
    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate PowerMonitor LaunchAgents with the active project Python.")
    parser.add_argument("--python", dest="python_path", type=Path, default=None, help="Python executable to use. Defaults to .venv/bin/python when present.")
    parser.add_argument("--launch-agents-dir", type=Path, default=LAUNCH_AGENTS_DIR)
    parser.add_argument("--only", choices=["collector", "web", "ocpp"], action="append", help="Generate only the named LaunchAgent. Can be repeated.")
    args = parser.parse_args()

    selected = tuple(args.only) if args.only else ("collector", "web", "ocpp")
    python_path = (args.python_path or discover_python()).resolve()
    if not python_path.exists():
        raise SystemExit(f"Python executable not found: {python_path}")
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
