#!/usr/bin/env python3
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from collect_today import AuthenticationFailure, COOKIE_PATH, fetch_solaredge, validate_response


def main() -> int:
    if not COOKIE_PATH.exists() or not COOKIE_PATH.read_text(encoding="utf-8").strip():
        print("expired/auth failure")
        return 1

    session_header = COOKIE_PATH.read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory(prefix="powermonitor-cookie-test-") as temporary:
        response_path = Path(temporary) / "response.json"
        try:
            fetch_solaredge(session_header, date.today(), response_path)
            validate_response(response_path, date.today())
        except AuthenticationFailure:
            print("expired/auth failure")
            return 1
        except subprocess.SubprocessError:
            print("network failure")
            return 2
        except (OSError, ValueError):
            print("network failure")
            return 2

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
