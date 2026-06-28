import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import web


class WebConfigTests(unittest.TestCase):
    def test_defaults_preserve_development_bind(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_settings = Path(temp_dir) / "missing.json"
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    web.resolve_web_bind(missing_settings),
                    ("127.0.0.1", 8765),
                )

    def test_config_file_can_set_host_and_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            settings.write_text(
                json.dumps({"web_server": {"host": "0.0.0.0", "port": 9876}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(web.resolve_web_bind(settings), ("0.0.0.0", 9876))

    def test_legacy_flat_config_keys_are_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            settings.write_text(
                json.dumps({"web_host": "0.0.0.0", "web_port": "9877"}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(web.resolve_web_bind(settings), ("0.0.0.0", 9877))

    def test_environment_overrides_config_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            settings.write_text(
                json.dumps({"web_server": {"host": "127.0.0.1", "port": 8765}}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"POWMONITOR_HOST": "0.0.0.0", "POWMONITOR_PORT": "9999"},
                clear=True,
            ):
                self.assertEqual(web.resolve_web_bind(settings), ("0.0.0.0", 9999))

    def test_invalid_port_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            settings.write_text(
                json.dumps({"web_server": {"host": "127.0.0.1", "port": "nope"}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError):
                    web.resolve_web_bind(settings)


if __name__ == "__main__":
    unittest.main()
