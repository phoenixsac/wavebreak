"""Local install API (lab devices only; never enabled in the field).

POST /install  body = bundle tar (raw bytes), optional header X-Sha256 → install result JSON
GET  /status   → fw_version, slot, unit state, restarts, last install result
Auth: `Authorization: Bearer <LAB_DEVICE_TOKEN>` on every request.
"""

from __future__ import annotations

import hmac
import json
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import log
from .install import APP_UNIT, Installer

MAX_BUNDLE_BYTES = 64 * 1024 * 1024


def status(installer: Installer) -> dict:
    return {
        "fw_version": installer.installed_version(),
        "slot": installer.current_slot(),
        "unit_state": installer.systemd.active_state(APP_UNIT),
        "restarts": installer.systemd.n_restarts(APP_UNIT),
        "last_install": installer.last_result(),
    }


def make_handler(installer: Installer, token: str, download_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ota-agent-local"

        def log_message(self, fmt, *args):  # route access logs through JSON logging
            log.event("local_api_request", method=self.command, path=self.path,
                      client=self.client_address[0])

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            if token and hmac.compare_digest(got, f"Bearer {token}"):
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def do_GET(self):
            if not self._authorized():
                return
            if self.path.split("?", 1)[0] == "/status":
                self._send(200, status(installer))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            if self.path.split("?", 1)[0] != "/install":
                self._send(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BUNDLE_BYTES:
                self._send(400, {"error": f"body must be a bundle tar of 1..{MAX_BUNDLE_BYTES} bytes"})
                return
            download_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=download_dir, suffix=".tar", delete=False) as f:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(remaining, 1 << 16))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
                bundle = Path(f.name)
            try:
                result = installer.install(bundle, expected_sha256=self.headers.get("X-Sha256"))
            finally:
                bundle.unlink(missing_ok=True)
            self._send(200 if result.ok else 422, result.to_dict())

    return Handler


def serve(installer: Installer, token: str, host: str = "0.0.0.0", port: int = 8081,
          download_dir: Path = Path("/var/lib/ota-agent/downloads")) -> None:
    if not token:
        raise SystemExit("LAB_DEVICE_TOKEN is required for the local API")
    httpd = ThreadingHTTPServer((host, port), make_handler(installer, token, download_dir))
    log.event("local_api_start", host=host, port=port)
    httpd.serve_forever()
