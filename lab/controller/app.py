"""Token-protected rehearsal lab controller for isolated container devices."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from wavebreak_clients._http import request_json
from wavebreak_clients.hawkbit import HawkbitClient

app = FastAPI(title="Wavebreak Lab Controller", version="0.1.0")
LOGGER = logging.getLogger("wavebreak.lab")
DEVICE_IMAGE = os.environ.get("LAB_DEVICE_IMAGE", "wavebreak-device:local")
LAB_NETWORK = os.environ.get("LAB_DOCKER_NETWORK", "wavebreak_lab")
DEVICE_TOKEN = os.environ.get("LAB_DEVICE_TOKEN", "")
API_TOKEN = os.environ.get("LAB_API_TOKEN", "")
HAWKBIT_URL = os.environ.get("HAWKBIT_URL", "http://hawkbit:8080")
HAWKBIT_AUTH = (
    os.environ.get("HAWKBIT_LAB_USERNAME", "lab"),
    os.environ.get("HAWKBIT_LAB_PASSWORD", "lab"),
)
SAMPLES: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=60))
DEVICE_ID_RE = re.compile(r"^lab-(\d+)$")


class CreateDevices(BaseModel):
    count: int = Field(default=1, ge=1, le=4)
    hw_rev: Literal["A", "B"]
    version: str = Field(pattern=r"^v1\.[0-4]$")


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    if not API_TOKEN:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "LAB_API_TOKEN is not configured")
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {API_TOKEN}"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")


def docker_client():
    try:
        return docker.from_env()
    except DockerException as exc:
        raise HTTPException(503, f"Docker is unavailable: {exc}") from exc


def get_container(device_id: str):
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise HTTPException(404, "lab device not found")
    try:
        container = docker_client().containers.get(device_id)
    except NotFound as exc:
        raise HTTPException(404, "lab device not found") from exc
    if container.labels.get("com.wavebreak.lab") != "true":
        raise HTTPException(404, "lab device not found")
    return container


def lab_containers() -> list:
    return docker_client().containers.list(
        all=True, filters={"label": "com.wavebreak.lab=true"}
    )


def allocate_device_id() -> str:
    numbers = []
    for container in lab_containers():
        match = DEVICE_ID_RE.fullmatch(container.name)
        if match:
            numbers.append(int(match.group(1)))
    return f"lab-{max(numbers, default=0) + 1:03d}"


def hawkbit() -> HawkbitClient:
    return HawkbitClient(HAWKBIT_URL, *HAWKBIT_AUTH, timeout=20)


def fetch_bundle(version: str) -> tuple[bytes, str]:
    """Read-only fetch of the selected release tarball from hawkBit."""
    client = hawkbit()
    query = f"name==wavebreak-app;version=={version}"
    modules = request_json(
        "GET", f"{client.api}/softwaremodules", params={"q": query, "limit": 20},
        auth=client.auth, timeout=client.timeout,
    ).get("content", [])
    module = next((item for item in modules if item.get("version") == version), None)
    if module is None:
        raise HTTPException(404, f"hawkBit bundle {version} was not found")
    artifacts = request_json(
        "GET", f"{client.api}/softwaremodules/{int(module['id'])}/artifacts",
        auth=client.auth, timeout=client.timeout,
    )
    artifact = next((item for item in artifacts if item.get("providedFilename", "").endswith(".tar")), None)
    if artifact is None:
        raise HTTPException(404, f"hawkBit bundle artifact for {version} was not found")
    expected = artifact.get("hashes", {}).get("sha256")
    if not expected:
        raise HTTPException(502, f"hawkBit bundle {version} has no SHA-256")
    with tempfile.TemporaryDirectory(prefix="wavebreak-lab-") as tmp:
        path = client.download_artifact(module["id"], artifact["id"], Path(tmp) / "bundle.tar")
        data = path.read_bytes()
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected):
        raise HTTPException(502, f"hawkBit bundle {version} failed SHA-256 verification")
    return data, expected


def create_container(device_id: str, hw_rev: str) -> Any:
    client = docker_client()
    try:
        client.images.get(DEVICE_IMAGE)
        client.networks.get(LAB_NETWORK)
    except ImageNotFound as exc:
        raise HTTPException(503, f"device image {DEVICE_IMAGE} is missing; build it first") from exc
    except NotFound as exc:
        raise HTTPException(503, f"lab network {LAB_NETWORK} is missing; start the lab service") from exc
    labels = {
        "com.wavebreak.lab": "true",
        "com.wavebreak.device_id": device_id,
        "com.wavebreak.hw_rev": hw_rev,
    }
    environment = {
        "DEVICE_ID": device_id,
        "HW_REV": hw_rev,
        "REGION": "lab",
        "RUNTIME": "container",
        "OTA_MODE": "local",
        "TELEMETRY": "0",
        "LAB_DEVICE_TOKEN": DEVICE_TOKEN,
        "APP_MEMORY_MAX": "48M",
        "WAVEBREAK_FRAME_SCALE": "1.0",
    }
    host_config = client.api.create_host_config(
        network_mode=LAB_NETWORK,
        binds={"/sys/fs/cgroup": {"bind": "/sys/fs/cgroup", "mode": "rw"}},
        tmpfs={"/run": "", "/run/lock": ""},
        mem_limit="256m",
        memswap_limit="256m",
        cgroupns="host",
        restart_policy={"Name": "unless-stopped"},
    )
    try:
        response = client.api.create_container(
            DEVICE_IMAGE, name=device_id, hostname=device_id,
            environment=environment, labels=labels, host_config=host_config,
        )
        client.api.start(response["Id"])
        return client.containers.get(response["Id"])
    except APIError as exc:
        raise HTTPException(502, f"failed to start lab device: {exc}") from exc


def device_request(device_id: str, method: str, path: str, data: bytes | None = None,
                   sha256: str | None = None) -> dict[str, Any]:
    if not DEVICE_TOKEN:
        raise HTTPException(503, "LAB_DEVICE_TOKEN is not configured")
    headers = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
    if sha256:
        headers["X-Sha256"] = sha256
    req = urllib.request.Request(
        f"http://{device_id}:8081{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=60 if method == "POST" else 5) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        detail = raw.decode(errors="replace")[:400]
        raise HTTPException(502, f"lab device {device_id} returned {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HTTPException(503, f"lab device {device_id} is not ready: {exc}") from exc
    return json.loads(raw) if raw else {}


def wait_device(device_id: str, wanted_version: str, bundle: tuple[bytes, str] | None) -> dict:
    deadline = time.monotonic() + 45
    last_error = None
    while time.monotonic() < deadline:
        try:
            state = device_request(device_id, "GET", "/status")
            if state.get("fw_version") == wanted_version:
                return state
            if bundle is not None:
                data, sha = bundle
                result = device_request(device_id, "POST", "/install", data=data, sha256=sha)
                if not result.get("ok"):
                    raise HTTPException(502, f"initial lab install failed: {result.get('message', result)}")
                return device_request(device_id, "GET", "/status")
        except HTTPException as exc:
            last_error = exc.detail
            if exc.status_code not in (502, 503):
                raise
        time.sleep(1)
    raise HTTPException(504, f"lab device {device_id} did not become ready: {last_error}")


def metric_value(text: str, metric: str) -> float | None:
    for line in text.splitlines():
        if line.startswith(metric):
            try:
                return float(line.rsplit(None, 1)[1])
            except (IndexError, ValueError):
                return None
    return None


def oom_kills(container: Any) -> int:
    try:
        raw_cgroup = container.exec_run(
            ["systemctl", "show", "inference-app", "-p", "ControlGroup", "--value"],
            demux=False,
        ).output.decode().strip()
        if not raw_cgroup.startswith("/"):
            return 0
        events = container.exec_run(
            ["cat", f"/sys/fs/cgroup{raw_cgroup}/memory.events"], demux=False
        ).output.decode()
        return int(next((line.split()[1] for line in events.splitlines() if line.startswith("oom_kill ")), "0"))
    except (APIError, AttributeError, IndexError, ValueError):
        return 0


def summary(device_id: str) -> dict[str, Any]:
    container = get_container(device_id)
    try:
        state = device_request(device_id, "GET", "/status")
        metrics_raw = urllib.request.urlopen(f"http://{device_id}:9100/metrics", timeout=5).read().decode()
    except (HTTPException, urllib.error.URLError, TimeoutError, OSError) as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(503, f"could not read lab device {device_id}: {exc}") from exc
    memory = metric_value(metrics_raw, "wavebreak_app_cgroup_memory_bytes")
    sample = {"ts": datetime.now(UTC).isoformat(), "memory_bytes": memory}
    SAMPLES[device_id].append(sample)
    container.reload()
    return {
        "id": device_id,
        "hw_rev": container.labels.get("com.wavebreak.hw_rev"),
        "fw_version": state.get("fw_version"),
        "memory_trend": list(SAMPLES[device_id]),
        "memory_bytes": memory,
        "restarts": state.get("restarts", 0),
        "oom_kills": oom_kills(container),
        "unit_state": state.get("unit_state"),
        "last_install_result": state.get("last_install"),
        "container_state": container.status,
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/lab/devices", dependencies=[Depends(require_token)])
def create_devices(body: CreateDevices) -> dict[str, Any]:
    bundle = fetch_bundle(body.version) if body.version != "v1.0" else None
    created = []
    created_ids = []
    try:
        for _ in range(body.count):
            device_id = allocate_device_id()
            create_container(device_id, body.hw_rev)
            created_ids.append(device_id)
            state = wait_device(device_id, body.version, bundle)
            created.append({"id": device_id, "hw_rev": body.hw_rev, "fw_version": state.get("fw_version")})
    except Exception:
        for device_id in created_ids:
            try:
                get_container(device_id).remove(force=True)
            except DockerException:
                LOGGER.exception("failed to clean up partial lab device %s", device_id)
        raise
    return {"devices": created}


@app.get("/lab/devices", dependencies=[Depends(require_token)])
def list_devices() -> dict[str, Any]:
    devices = []
    for container in lab_containers():
        container.reload()
        devices.append({"id": container.name, "state": container.status,
                        "hw_rev": container.labels.get("com.wavebreak.hw_rev")})
    return {"devices": devices}


@app.get("/lab/devices/{device_id}/summary", dependencies=[Depends(require_token)])
def get_summary(device_id: str) -> dict[str, Any]:
    return summary(device_id)


@app.post("/lab/devices/{device_id}/install", dependencies=[Depends(require_token)])
def install(device_id: str, body: dict[str, str]) -> dict[str, Any]:
    get_container(device_id)
    version = body.get("version", "")
    if not re.fullmatch(r"v1\.[0-4]", version):
        raise HTTPException(422, "version must be v1.0 through v1.4")
    bundle = fetch_bundle(version)
    state = device_request(device_id, "GET", "/status")
    if state.get("fw_version") == version:
        return {"ok": True, "id": device_id, "fw_version": version, "message": "already installed"}
    result = device_request(device_id, "POST", "/install", data=bundle[0], sha256=bundle[1])
    if not result.get("ok"):
        raise HTTPException(422, result)
    return {"ok": True, "id": device_id, "fw_version": version, "install": result}


@app.delete("/lab/devices/{device_id}", dependencies=[Depends(require_token)])
def delete_device(device_id: str) -> dict[str, str]:
    container = get_container(device_id)
    container.remove(force=True)
    SAMPLES.pop(device_id, None)
    return {"id": device_id, "deleted": "true"}
