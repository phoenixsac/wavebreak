"""Tests for the DDI (field) client against a mock hawkBit server.

The mock server (FakeHawkbit + make_ddi_handler) reproduces the real response shapes captured
from hawkBit 1.1.0 for the controller base doc, deploymentBase, feedback and artifact endpoints,
so DdiAgent is exercised through real HTTP requests (no mocking of ota_agent internals).
"""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import make_bundle
from ota_agent.ddi import DdiAgent, DdiConfig, action_id_from_href, parse_sleep
from ota_agent.install import sha256_file

TOKEN = "tok123"
CONTROLLER_ID = "edge-001"
IDENTITY = {"DEVICE_ID": CONTROLLER_ID, "HW_REV": "B", "REGION": "eu-west"}


class FakeHawkbit:
    """Server-side state for the mock DDI endpoint, mutated by tests."""

    def __init__(self, token: str, controller_id: str):
        self.token = token
        self.controller_id = controller_id
        self.base_url: str | None = None  # filled in once the server has bound a port
        self.mode = "unregistered"  # unregistered | deployment | cancel
        self.deployment_json: dict | None = None
        self.action_id: str | None = None
        self.artifacts: dict[str, bytes] = {}
        self.config_data_log: list[dict] = []
        self.feedback_log: list[tuple[str, str, dict]] = []
        self.artifact_requests = 0
        self.lock = threading.Lock()

    def controller_base(self) -> str:
        return f"{self.base_url}/DEFAULT/controller/v1/{self.controller_id}"

    def set_unregistered(self) -> None:
        self.mode = "unregistered"

    def set_deployment(self, deployment_json: dict, action_id: str = "1") -> None:
        self.mode = "deployment"
        self.deployment_json = deployment_json
        self.action_id = action_id

    def set_cancel(self, action_id: str = "7") -> None:
        self.mode = "cancel"
        self.action_id = action_id

    def base_doc(self) -> dict:
        links: dict = {}
        if self.mode == "unregistered":
            links = {"configData": {"href": f"{self.controller_base()}/configData"}}
        elif self.mode == "deployment":
            href = f"{self.controller_base()}/deploymentBase/{self.action_id}?c=411599879"
            links = {"deploymentBase": {"href": href}}
        elif self.mode == "cancel":
            links = {"cancelAction": {"href": f"{self.controller_base()}/cancelAction/{self.action_id}"}}
        return {"config": {"polling": {"sleep": "00:00:10"}}, "_links": links}


def make_deployment(state: FakeHawkbit, action_id: str, version: str, bundle, sha: str) -> dict:
    """Build a deploymentBase body shaped like hawkBit 1.1.0, with an artifact for `bundle`."""
    filename = bundle.name
    href = f"{state.controller_base()}/softwaremodules/2/artifacts/{filename}"
    return {
        "id": action_id,
        "deployment": {
            "download": "forced",
            "update": "forced",
            "chunks": [
                {
                    "part": "bApp",
                    "version": version,
                    "name": "wavebreak-app",
                    "artifacts": [
                        {
                            "filename": filename,
                            "hashes": {"sha256": sha},
                            "size": bundle.stat().st_size,
                            "_links": {"download-http": {"href": href}},
                        }
                    ],
                }
            ],
        },
    }


def make_ddi_handler(state: FakeHawkbit):
    base = f"/DEFAULT/controller/v1/{state.controller_id}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silence test output
            pass

        def _authorized(self) -> bool:
            expected = f"GatewayToken {state.token}"
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got, expected):
                return True
            self._send_json(401, {"error": "unauthorized"})
            return False

        def _send_json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_empty(self, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw) if raw else {}

        def do_GET(self):
            if not self._authorized():
                return
            path = self.path.split("?", 1)[0]
            if path == base:
                self._send_json(200, state.base_doc())
            elif path.startswith(base + "/deploymentBase/"):
                self._send_json(200, state.deployment_json)
            elif path.startswith(base + "/softwaremodules/"):
                filename = path.rsplit("/", 1)[-1]
                with state.lock:
                    state.artifact_requests += 1
                data = state.artifacts.get(filename)
                if data is None:
                    self._send_json(404, {"error": "not found"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send_json(404, {"error": "not found"})

        def do_PUT(self):
            if not self._authorized():
                return
            path = self.path.split("?", 1)[0]
            if path == base + "/configData":
                body = self._read_json_body()
                with state.lock:
                    state.config_data_log.append(body)
                self._send_empty(200)
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            path = self.path.split("?", 1)[0]
            body = self._read_json_body()
            if path.startswith(base + "/deploymentBase/") and path.endswith("/feedback"):
                action_id = path[len(base + "/deploymentBase/"): -len("/feedback")]
                with state.lock:
                    state.feedback_log.append(("deploymentBase", action_id, body))
                self._send_empty(200)
            elif path.startswith(base + "/cancelAction/") and path.endswith("/feedback"):
                action_id = path[len(base + "/cancelAction/"): -len("/feedback")]
                with state.lock:
                    state.feedback_log.append(("cancelAction", action_id, body))
                self._send_empty(200)
            else:
                self._send_json(404, {"error": "not found"})

    return Handler


@pytest.fixture
def ddi_server():
    state = FakeHawkbit(token=TOKEN, controller_id=CONTROLLER_ID)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_ddi_handler(state))
    state.base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def make_cfg(state: FakeHawkbit, tmp_path, token: str = TOKEN) -> DdiConfig:
    return DdiConfig(
        base_url=state.base_url,
        tenant="DEFAULT",
        controller_id=state.controller_id,
        token=token,
        download_dir=tmp_path / "downloads",
        min_backoff_s=1.0,
        max_backoff_s=2.0,
        timeout_s=5.0,
    )


def make_agent(state, installer, tmp_path, token=TOKEN):
    cfg = make_cfg(state, tmp_path, token=token)
    return DdiAgent(cfg, installer, sleep=lambda s: None, identity=dict(IDENTITY))


# --- registration ------------------------------------------------------------------------


def test_registration_sends_config_data_and_returns_sleep(ddi_server, device):
    installer, _systemd, tmp = device
    ddi_server.set_unregistered()
    agent = make_agent(ddi_server, installer, tmp)

    delay = agent.run_once()

    assert delay == 10.0
    assert len(ddi_server.config_data_log) == 1
    body = ddi_server.config_data_log[0]
    assert body["mode"] == "merge"
    assert body["data"] == {
        "device_id": "edge-001",
        "hw_rev": "B",
        "region": "eu-west",
        "fw_version": "v1.0",
    }


# --- deployment ---------------------------------------------------------------------------


def test_deployment_happy_path_installs_and_reports_success(ddi_server, device):
    installer, _systemd, tmp = device
    bundle = make_bundle(tmp, "v1.1")
    sha = sha256_file(bundle)
    ddi_server.artifacts[bundle.name] = bundle.read_bytes()
    ddi_server.set_deployment(make_deployment(ddi_server, "1", "v1.1", bundle, sha), action_id="1")
    agent = make_agent(ddi_server, installer, tmp)

    delay = agent.run_once()

    assert delay == 10.0
    assert installer.installed_version() == "v1.1"
    kinds = [(k, a, f["status"]["execution"], f["status"]["result"]["finished"]) for k, a, f in
             ddi_server.feedback_log]
    assert kinds == [
        ("deploymentBase", "1", "proceeding", "none"),
        ("deploymentBase", "1", "proceeding", "none"),
        ("deploymentBase", "1", "closed", "success"),
    ]
    # attributes reported once on registration, once after the install (fw_version changed)
    assert len(ddi_server.config_data_log) == 2
    assert ddi_server.config_data_log[-1]["data"]["fw_version"] == "v1.1"


def test_sha256_mismatch_closes_failure_and_leaves_version(ddi_server, device):
    installer, _systemd, tmp = device
    bundle = make_bundle(tmp, "v1.2")
    ddi_server.artifacts[bundle.name] = bundle.read_bytes()
    wrong_sha = "0" * 64
    ddi_server.set_deployment(make_deployment(ddi_server, "1", "v1.2", bundle, wrong_sha))
    agent = make_agent(ddi_server, installer, tmp)

    agent.run_once()

    assert installer.installed_version() == "v1.0"
    kind, action_id, body = ddi_server.feedback_log[-1]
    assert (kind, action_id) == ("deploymentBase", "1")
    assert body["status"]["execution"] == "closed"
    assert body["status"]["result"]["finished"] == "failure"
    assert "sha256 mismatch" in body["status"]["details"][0]


def test_install_failure_crash_closes_failure_with_rollback(ddi_server, device):
    installer, systemd, tmp = device
    systemd.behaviour["v9.9"] = "crash"
    bundle = make_bundle(tmp, "v9.9")
    sha = sha256_file(bundle)
    ddi_server.artifacts[bundle.name] = bundle.read_bytes()
    ddi_server.set_deployment(make_deployment(ddi_server, "1", "v9.9", bundle, sha))
    agent = make_agent(ddi_server, installer, tmp)

    agent.run_once()

    assert installer.installed_version() == "v1.0"
    kind, action_id, body = ddi_server.feedback_log[-1]
    assert (kind, action_id) == ("deploymentBase", "1")
    assert body["status"]["execution"] == "closed"
    assert body["status"]["result"]["finished"] == "failure"
    assert "rolled back to v1.0" in body["status"]["details"][0]


def test_same_version_already_installed_skips_download(ddi_server, device):
    installer, _systemd, tmp = device
    bundle = make_bundle(tmp, "v1.0")
    # deliberately do not register the artifact bytes: a download attempt would 404 and fail run_once
    ddi_server.set_deployment(make_deployment(ddi_server, "1", "v1.0", bundle, sha256_file(bundle)))
    agent = make_agent(ddi_server, installer, tmp)

    delay = agent.run_once()

    assert delay == 10.0
    assert ddi_server.artifact_requests == 0
    assert len(ddi_server.feedback_log) == 1
    kind, action_id, body = ddi_server.feedback_log[0]
    assert (kind, action_id) == ("deploymentBase", "1")
    assert body["status"]["execution"] == "closed"
    assert body["status"]["result"]["finished"] == "success"
    assert "already installed" in body["status"]["details"][0]


# --- auth / errors ------------------------------------------------------------------------


def test_wrong_token_backs_off_and_counts_failure(ddi_server, device):
    installer, _systemd, tmp = device
    ddi_server.set_unregistered()
    agent = make_agent(ddi_server, installer, tmp, token="wrong-token")

    delay = agent.run_once()

    assert delay > 0
    assert agent.failures == 1


# --- cancel ------------------------------------------------------------------------------


def test_cancel_action_reports_closed_success(ddi_server, device):
    installer, _systemd, tmp = device
    ddi_server.set_cancel(action_id="7")
    agent = make_agent(ddi_server, installer, tmp)

    agent.run_once()

    kind, action_id, body = ddi_server.feedback_log[-1]
    assert (kind, action_id) == ("cancelAction", "7")
    assert body["status"]["execution"] == "closed"
    assert body["status"]["result"]["finished"] == "success"


# --- pure helpers --------------------------------------------------------------------------


def test_parse_sleep():
    assert parse_sleep("00:00:10") == 10.0
    assert parse_sleep("00:01:30") == 90.0
    assert parse_sleep(None) == 30.0
    assert parse_sleep("garbage") == 30.0
    assert parse_sleep(None, default=5.0) == 5.0


def test_action_id_from_href():
    assert action_id_from_href("http://h/x/deploymentBase/42?c=-123") == "42"
    assert action_id_from_href("http://h/x/cancelAction/7") == "7"
    assert action_id_from_href("http://h/x/deploymentBase/42/") == "42"
