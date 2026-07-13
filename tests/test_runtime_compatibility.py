import plistlib
import sys
import tempfile
import unittest
import uuid
import inspect
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import install_launch_agents
import validate_launch_agents
import web
from engine.runtime_diagnostics import runtime_diagnostics


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_multipart_parser_reads_upload_without_cgi(self):
        boundary = f"----PowerMonitor{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="import_file"; filename="ocular.xlsx"\r\n'
            "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
        ).encode("utf-8") + b"excel-bytes" + f"\r\n--{boundary}--\r\n".encode("utf-8")

        fields, files = web.parse_multipart_form(body, f"multipart/form-data; boundary={boundary}")

        self.assertEqual(fields, {})
        self.assertEqual(files["import_file"]["filename"], "ocular.xlsx")
        self.assertEqual(files["import_file"]["content"], b"excel-bytes")

    def test_multipart_parser_reads_form_fields(self):
        boundary = f"----PowerMonitor{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="entity_mappings.solar_power"\r\n\r\n'
            "sensor.solar\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="username"\r\n\r\n'
            "user@example.com\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        fields, files = web.parse_multipart_form(body, f"multipart/form-data; boundary={boundary}")

        self.assertEqual(files, {})
        self.assertEqual(fields["entity_mappings.solar_power"], "sensor.solar")
        self.assertEqual(fields["username"], "user@example.com")
        self.assertEqual(
            web.normalize_provider_payload(fields),
            {"username": "user@example.com", "entity_mappings": {"solar_power": "sensor.solar"}},
        )

    def test_web_module_does_not_import_deprecated_cgi(self):
        self.assertNotIn("import cgi", inspect.getsource(web))

    def test_runtime_diagnostics_reports_environment_and_missing_optional_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "data" / "power.sqlite"
            with patch("engine.runtime_diagnostics._module_available", return_value=False):
                data = runtime_diagnostics(project_root=root, database_path=db_path, web_host="0.0.0.0", web_port=8765)

        self.assertEqual(data["web_host"], "0.0.0.0")
        self.assertEqual(data["web_port"], 8765)
        self.assertIn("python_version", data)
        self.assertFalse(data["playwright_available"])
        self.assertFalse(data["pypdf_available"])
        self.assertIn("Playwright is not installed", " ".join(data["warnings"]))
        self.assertIn("pypdf is not installed", " ".join(data["warnings"]))

    def test_launch_agent_generator_uses_project_venv_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "PowerMonitor"
            python_path = root / ".venv" / "bin" / "python"
            python_path.parent.mkdir(parents=True)
            python_path.write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "collector").mkdir()
            (root / "collector" / "collect_today.py").write_text("", encoding="utf-8")
            (root / "collector" / "web.py").write_text("", encoding="utf-8")
            launch_dir = Path(temporary) / "LaunchAgents"

            self.assertEqual(install_launch_agents.discover_python(root), python_path.resolve())
            written = install_launch_agents.write_launch_agents(project_root=root, launch_agents_dir=launch_dir, selected=("collector", "web"))

            self.assertEqual(len(written), 2)
            for path in written:
                with path.open("rb") as handle:
                    plist = plistlib.load(handle)
                self.assertEqual(plist["ProgramArguments"][0], str(python_path.resolve()))
                self.assertNotEqual(plist["ProgramArguments"][0], "/usr/bin/python3")

    def test_launch_agent_generator_removes_obsolete_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            launch_dir = Path(temporary) / "LaunchAgents"
            launch_dir.mkdir()
            old_plist = launch_dir / "com.penrite.powermonitor.plist"
            with old_plist.open("wb") as handle:
                plistlib.dump({"Label": "com.penrite.powermonitor"}, handle)

            with patch("install_launch_agents.subprocess.run") as run:
                messages = install_launch_agents.remove_obsolete_launch_agents(launch_dir)

            self.assertFalse(old_plist.exists())
            self.assertTrue(any("Removed obsolete plist" in message for message in messages))
            run.assert_called()

    def test_launch_agent_validator_detects_duplicate_obsolete_collector(self):
        with tempfile.TemporaryDirectory() as temporary:
            launch_dir = Path(temporary) / "LaunchAgents"
            launch_dir.mkdir()
            supported = launch_dir / "com.local.powermonitor.collector.plist"
            obsolete = launch_dir / "com.penrite.powermonitor.plist"
            for path, label in (
                (supported, "com.local.powermonitor.collector"),
                (obsolete, "com.penrite.powermonitor"),
            ):
                with path.open("wb") as handle:
                    plistlib.dump(
                        {
                            "Label": label,
                            "ProgramArguments": [sys.executable, str(PROJECT_ROOT / "collector" / "collect_today.py")],
                            "WorkingDirectory": str(PROJECT_ROOT),
                        },
                        handle,
                    )

            with patch("validate_launch_agents.launchctl_labels", return_value=[]):
                result = validate_launch_agents.validate(launch_dir)

            self.assertFalse(result["ok"])
            self.assertTrue(any("duplicate LaunchAgent plists for collector" in issue for issue in result["issues"]))
            self.assertTrue(any("obsolete namespace plist present" in issue for issue in result["issues"]))


if __name__ == "__main__":
    unittest.main()
