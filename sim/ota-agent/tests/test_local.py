"""Tests for the local install API (lab mode) via real HTTP against ThreadingHTTPServer."""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest
from conftest import make_bundle
from ota_agent.install import APP_UNIT, sha256_file
from ota_agent.local import make_handler

TOKEN = "lab-secret"


@pytest.fixture
def local_server(device):
    installer, systemd, tmp = device
    handler = make_handler(installer, TOKEN, tmp / "downloads")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], installer, systemd, tmp
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _conn(port):
    return http.client.HTTPConnection("127.0.0.1", port, timeout=5)


def test_get_status_requires_auth(local_server):
    port, *_ = local_server
    conn = _conn(port)
    conn.request("GET", "/status")
    resp = conn.getresponse()
    assert resp.status == 401
    resp.read()
    conn.close()


def test_get_status_wrong_token(local_server):
    port, *_ = local_server
    conn = _conn(port)
    conn.request("GET", "/status", headers={"Authorization": "Bearer nope"})
    resp = conn.getresponse()
    assert resp.status == 401
    resp.read()
    conn.close()


def test_get_status_fields(local_server):
    port, _installer, systemd, _tmp = local_server
    conn = _conn(port)
    conn.request("GET", "/status", headers={"Authorization": f"Bearer {TOKEN}"})
    resp = conn.getresponse()
    assert resp.status == 200

    body = json.loads(resp.read())
    conn.close()
    assert body["fw_version"] == "v1.0"
    assert body["slot"] == "a"
    assert body["unit_state"] == systemd.active_state(APP_UNIT)
    assert body["restarts"] == systemd.n_restarts(APP_UNIT)
    assert body["last_install"] is None


def test_post_install_good_bundle_returns_ok(local_server):
    port, installer, _systemd, tmp = local_server
    bundle = make_bundle(tmp, "v1.1")
    data = bundle.read_bytes()
    sha = sha256_file(bundle)
    conn = _conn(port)
    conn.request(
        "POST",
        "/install",
        body=data,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Length": str(len(data)),
            "X-Sha256": sha,
        },
    )
    resp = conn.getresponse()

    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 200
    assert body["ok"] is True
    assert body["version"] == "v1.1"
    assert installer.installed_version() == "v1.1"


def test_post_install_crash_bundle_returns_422(local_server):
    port, installer, systemd, tmp = local_server
    systemd.behaviour["v9.9"] = "crash"
    bundle = make_bundle(tmp, "v9.9")
    data = bundle.read_bytes()
    conn = _conn(port)
    conn.request(
        "POST",
        "/install",
        body=data,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Length": str(len(data))},
    )
    resp = conn.getresponse()

    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 422
    assert body["ok"] is False
    assert installer.installed_version() == "v1.0"


def test_post_install_sha_mismatch_returns_422(local_server):
    port, installer, _systemd, tmp = local_server
    bundle = make_bundle(tmp, "v1.1")
    data = bundle.read_bytes()
    conn = _conn(port)
    conn.request(
        "POST",
        "/install",
        body=data,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Length": str(len(data)),
            "X-Sha256": "0" * 64,
        },
    )
    resp = conn.getresponse()

    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 422
    assert body["ok"] is False
    assert "sha256 mismatch" in body["message"]
    assert installer.installed_version() == "v1.0"


def test_post_install_empty_body_returns_400(local_server):
    port, *_ = local_server
    conn = _conn(port)
    conn.request(
        "POST",
        "/install",
        body=b"",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Length": "0"},
    )
    resp = conn.getresponse()
    assert resp.status == 400
    resp.read()
    conn.close()


def test_unknown_path_returns_404(local_server):
    port, *_ = local_server
    conn = _conn(port)
    conn.request("GET", "/nope", headers={"Authorization": f"Bearer {TOKEN}"})
    resp = conn.getresponse()
    assert resp.status == 404
    resp.read()
    conn.close()
