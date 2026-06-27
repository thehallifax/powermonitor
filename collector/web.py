#!/usr/bin/env python3
import json
import logging
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import report

HOST = "127.0.0.1"
PORT = 8765
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = PROJECT_ROOT / "reports" / "index.html"
LOG_PATH = PROJECT_ROOT / "logs" / "web.log"


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def live_health() -> dict:
    settings = report.load_settings()
    with sqlite3.connect(report.DB_PATH) as conn:
        report.validate_database(conn)
        return report.health_data(conn, report.local_today(), settings)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "PowerMonitor/1.0"

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        path = request.path
        params = parse_qs(request.query)
        try:
            if path == "/":
                self.serve_dashboard(params)
            elif path == "/reports/index.html":
                self.serve_static_report()
            elif path == "/api/summary":
                self.send_json(
                    report.dashboard_data(
                        period_kind=self.param(params, "period", "current_billing_period"),
                        start=self.param(params, "start"),
                        end=self.param(params, "end"),
                    )
                )
            elif path == "/api/compare":
                self.send_json(self.compare_data(params))
            elif path == "/api/health":
                self.send_json(live_health())
            else:
                self.send_json({"error": "not found"}, status=404)
        except (FileNotFoundError, ValueError, RuntimeError, sqlite3.Error) as exc:
            logging.error("Request failed for %s: %s", path, exc)
            self.send_json({"error": "dashboard data unavailable"}, status=500)

    @staticmethod
    def param(params: dict, name: str, default=None):
        values = params.get(name)
        return values[0] if values else default

    def compare_data(self, params: dict) -> dict:
        settings = report.load_settings()
        today = report.local_today()
        period_a = report.resolve_period(
            self.param(params, "period_a", "custom" if self.param(params, "start_a") else "current_billing_period"),
            settings,
            today,
            self.param(params, "start_a"),
            self.param(params, "end_a"),
        )
        period_b = report.resolve_period(
            self.param(params, "period_b", "custom" if self.param(params, "start_b") else "previous_billing_period"),
            settings,
            today,
            self.param(params, "start_b"),
            self.param(params, "end_b"),
        )
        return report.comparison_data(period_a, period_b, settings)

    def serve_dashboard(self, params: dict) -> None:
        period = self.param(params, "period", "current_billing_period")
        content = report.build_live_html(
            period,
            self.param(params, "start"),
            self.param(params, "end"),
        ).encode("utf-8")
        self.send_html(content)

    def serve_static_report(self) -> None:
        if not REPORT_PATH.exists():
            self.send_json(
                {"error": "dashboard has not been generated; run python3 collector/report.py"},
                status=503,
            )
            return
        self.send_html(REPORT_PATH.read_bytes())

    def send_html(self, content: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload: dict, status: int = 200) -> None:
        content = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(content)

    def send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "script-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def log_request(self, code="-", size="-") -> None:
        path = urlsplit(self.path).path
        logging.info("%s %s %s %s", self.client_address[0], self.command, path, code)

    def log_message(self, message: str, *args) -> None:
        return


def main() -> None:
    setup_logging()
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    logging.info("PowerMonitor dashboard server started on http://%s:%d", HOST, PORT)
    print(f"PowerMonitor dashboard: http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        logging.info("PowerMonitor dashboard server stopped")


if __name__ == "__main__":
    main()
