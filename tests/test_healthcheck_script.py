"""Tests for scripts/healthcheck.sh execution and exit codes."""

from __future__ import annotations

import http.server
import subprocess
import threading
from pathlib import Path
from typing import ClassVar

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "healthcheck.sh"


class _DummyHandler(http.server.BaseHTTPRequestHandler):
    status_by_path: ClassVar[dict[str, int]] = {
        "/health": 200,
        "/ready": 200,
    }

    def do_GET(self) -> None:
        status = self.status_by_path.get(self.path, 404)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"test"}')

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_healthcheck_script_invalid_mode_exits_2() -> None:
    """Invalid mode argument exits with code 2 and usage instructions."""
    result = subprocess.run(
        [str(SCRIPT_PATH), "invalid_mode"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_healthcheck_script_readiness_and_liveness_success() -> None:
    """Readiness and liveness exit 0 when endpoints return 200."""
    _DummyHandler.status_by_path = {"/health": 200, "/ready": 200}
    server = http.server.HTTPServer(("127.0.0.1", 0), _DummyHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        env = {"APP_HOST": "127.0.0.1", "APP_PORT": str(port)}
        res_live = subprocess.run(
            [str(SCRIPT_PATH), "liveness"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res_live.returncode == 0
        assert "OK: /health returned HTTP 200" in res_live.stdout

        res_ready = subprocess.run(
            [str(SCRIPT_PATH), "readiness"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res_ready.returncode == 0
        assert "OK: /ready returned HTTP 200" in res_ready.stdout
    finally:
        server.shutdown()
        server.server_close()


def test_healthcheck_script_readiness_failure_exits_1() -> None:
    """Readiness exits 1 when /ready returns 503 degraded."""
    _DummyHandler.status_by_path = {"/health": 200, "/ready": 503}
    server = http.server.HTTPServer(("127.0.0.1", 0), _DummyHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        env = {"APP_HOST": "127.0.0.1", "APP_PORT": str(port)}
        res_live = subprocess.run(
            [str(SCRIPT_PATH), "liveness"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res_live.returncode == 0

        res_ready = subprocess.run(
            [str(SCRIPT_PATH), "readiness"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res_ready.returncode == 1
        assert "FAIL: /ready returned HTTP 503" in res_ready.stderr
    finally:
        server.shutdown()
        server.server_close()


def test_healthcheck_script_all_mode_reports_both_endpoints_on_failure() -> None:
    """all mode checks and reports both /health and /ready even if /health fails."""
    _DummyHandler.status_by_path = {"/health": 500, "/ready": 200}
    server = http.server.HTTPServer(("127.0.0.1", 0), _DummyHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        env = {"APP_HOST": "127.0.0.1", "APP_PORT": str(port)}
        res_all = subprocess.run(
            [str(SCRIPT_PATH), "all"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res_all.returncode == 1
        assert "Checking liveness..." in res_all.stdout
        assert "FAIL: /health returned HTTP 500" in res_all.stderr
        assert "Checking readiness..." in res_all.stdout
        assert "OK: /ready returned HTTP 200" in res_all.stdout
    finally:
        server.shutdown()
        server.server_close()
