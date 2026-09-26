"""hawkBit DDI client (field mode).

Loop: poll the controller base → PUT configData (attributes) → on a deploymentBase link fetch the
action, download the artifact, install via the shared core, and send feedback. Endpoint shapes
follow the hawkBit DDI API (docs/architecture.md §5).
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import log
from .install import Installer, read_env_file

DEFAULT_SLEEP_S = 30.0


@dataclass
class DdiConfig:
    base_url: str  # e.g. http://hawkbit:8080
    tenant: str
    controller_id: str
    auth_mode: str = "gateway"  # gateway or target
    token: str = ""
    download_dir: Path = Path("/var/lib/ota-agent/downloads")
    min_backoff_s: float = 5.0
    max_backoff_s: float = 300.0
    timeout_s: float = 30.0

    @property
    def controller_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.tenant}/controller/v1/{self.controller_id}"

    def auth_header(self) -> str:
        scheme = "TargetToken" if self.auth_mode == "target" else "GatewayToken"
        return f"{scheme} {self.token}"


class DdiError(Exception):
    pass


def parse_sleep(value: str | None, default: float = DEFAULT_SLEEP_S) -> float:
    """hawkBit polling sleep is 'HH:MM:SS'."""
    if not value:
        return default
    m = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", value.strip())
    if not m:
        return default
    h, mi, s = (int(x) for x in m.groups())
    return float(h * 3600 + mi * 60 + s) or default


def action_id_from_href(href: str) -> str:
    """.../deploymentBase/42?c=-123 → '42'."""
    return href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


class DdiClient:
    """Minimal HTTP wrapper around the DDI endpoints."""

    def __init__(self, cfg: DdiConfig):
        self.cfg = cfg

    def _request(self, method: str, url: str, body: dict | None = None, accept_json: bool = True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self.cfg.auth_header())
        req.add_header("Accept", "application/hal+json, application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode(errors="replace")
            raise DdiError(f"{method} {url} -> HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise DdiError(f"{method} {url} -> {e}") from e
        if not accept_json or not raw:
            return None
        return json.loads(raw)

    def poll(self) -> dict:
        return self._request("GET", self.cfg.controller_url) or {}

    def put_config_data(self, attributes: dict) -> None:
        body = {"mode": "merge", "data": attributes}
        self._request("PUT", f"{self.cfg.controller_url}/configData", body, accept_json=False)

    def get(self, href: str) -> dict:
        return self._request("GET", href) or {}

    def feedback(self, kind: str, action_id: str, execution: str, finished: str, details: list[str]):
        """kind: deploymentBase or cancelAction."""
        body = {
            "status": {
                "execution": execution,
                "result": {"finished": finished},
                "details": details,
            }
        }
        url = f"{self.cfg.controller_url}/{kind}/{action_id}/feedback"
        self._request("POST", url, body, accept_json=False)

    def download(self, href: str, dest: Path) -> None:
        req = urllib.request.Request(href)
        req.add_header("Authorization", self.cfg.auth_header())
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp, open(tmp, "wb") as f:
                while chunk := resp.read(1 << 16):
                    f.write(chunk)
        except (urllib.error.URLError, OSError) as e:
            raise DdiError(f"download {href} failed: {e}") from e
        tmp.replace(dest)


def pick_artifact(deployment: dict) -> tuple[str, dict]:
    """Return (chunk version, artifact) for the single wavebreak-app artifact in the action."""
    chunks = deployment.get("deployment", {}).get("chunks", [])
    for chunk in chunks:
        for art in chunk.get("artifacts", []):
            if art.get("filename", "").endswith(".tar"):
                return chunk.get("version", ""), art
    raise DdiError("no .tar artifact in deploymentBase")


def artifact_href(art: dict) -> str:
    links = art.get("_links", {})
    for key in ("download-http", "download"):
        if key in links:
            return links[key]["href"]
    raise DdiError(f"artifact {art.get('filename')} has no download link")


class DdiAgent:
    def __init__(self, cfg: DdiConfig, installer: Installer, client: DdiClient | None = None,
                 *, sleep=time.sleep, identity: dict | None = None):
        self.cfg = cfg
        self.installer = installer
        self.client = client or DdiClient(cfg)
        self.sleep = sleep
        self.identity = identity if identity is not None else read_env_file(installer.paths.identity_env)
        self.reported_attrs: dict | None = None
        self.handled: set[str] = set()
        self.failures = 0

    def attributes(self) -> dict:
        return {
            "device_id": self.identity.get("DEVICE_ID", self.cfg.controller_id),
            "hw_rev": self.identity.get("HW_REV", "unknown"),
            "region": self.identity.get("REGION", "unknown"),
            "fw_version": self.installer.installed_version() or "unknown",
        }

    def report_attributes(self, force: bool = False) -> None:
        attrs = self.attributes()
        if force or attrs != self.reported_attrs:
            self.client.put_config_data(attrs)
            self.reported_attrs = attrs
            log.event("config_data_sent", attributes=attrs)

    def backoff(self) -> float:
        base = min(self.cfg.max_backoff_s, self.cfg.min_backoff_s * (2 ** min(self.failures, 10)))
        return base * random.uniform(0.8, 1.2)

    def run_once(self) -> float:
        """One poll cycle. Returns seconds to sleep before the next one."""
        try:
            base = self.client.poll()
            links = base.get("_links", {})
            if "configData" in links or self.reported_attrs is None:
                self.report_attributes(force="configData" in links)
            if "cancelAction" in links:
                self.handle_cancel(links["cancelAction"]["href"])
            elif "deploymentBase" in links:
                self.handle_deployment(links["deploymentBase"]["href"])
            self.failures = 0
            return parse_sleep(base.get("config", {}).get("polling", {}).get("sleep"))
        except DdiError as e:
            self.failures += 1
            delay = self.backoff()
            log.event("ddi_error", level="warning", error=str(e), retry_in_s=round(delay, 1))
            return delay

    def run_forever(self) -> None:
        log.event("ddi_start", controller=self.cfg.controller_url, auth_mode=self.cfg.auth_mode)
        while True:
            self.sleep(self.run_once())

    def handle_cancel(self, href: str) -> None:
        action_id = action_id_from_href(href)
        # Installs are synchronous, so a pending cancel always finds us idle: accept it.
        self.client.feedback("cancelAction", action_id, "closed", "success", ["cancel accepted (idle)"])
        log.event("action_canceled", action_id=action_id)

    def handle_deployment(self, href: str) -> None:
        action_id = action_id_from_href(href)
        if action_id in self.handled:
            return
        deployment = self.client.get(href)
        version, art = pick_artifact(deployment)
        installed = self.installer.installed_version()
        log.event("deployment_received", action_id=action_id, version=version, installed=installed)

        if version and version == installed:
            self.client.feedback("deploymentBase", action_id, "closed", "success",
                                 [f"{version} already installed; nothing to do"])
            self.handled.add(action_id)
            return

        self.client.feedback("deploymentBase", action_id, "proceeding", "none",
                             [f"downloading {art.get('filename')}"])
        dest = self.cfg.download_dir / art["filename"]
        try:
            self.client.download(artifact_href(art), dest)
        except DdiError as e:
            self.client.feedback("deploymentBase", action_id, "closed", "failure", [str(e)])
            self.handled.add(action_id)
            return
        expected = art.get("hashes", {}).get("sha256")
        self.client.feedback("deploymentBase", action_id, "proceeding", "none",
                             [f"downloaded; installing {version}"])
        result = self.installer.install(dest, expected_sha256=expected)
        dest.unlink(missing_ok=True)
        details = [result.message, f"slot={result.slot}", f"duration_s={result.duration_s}"]
        self.client.feedback("deploymentBase", action_id, "closed",
                             "success" if result.ok else "failure", details)
        self.handled.add(action_id)
        self.report_attributes()


def config_from_env(env: dict) -> DdiConfig:
    mode = env.get("DDI_AUTH_MODE", "gateway")
    token = env.get("HAWKBIT_TARGET_TOKEN" if mode == "target" else "HAWKBIT_GATEWAY_TOKEN", "")
    return DdiConfig(
        base_url=env.get("HAWKBIT_DDI_URL", "http://hawkbit:8080"),
        tenant=env.get("HAWKBIT_TENANT", "DEFAULT"),
        controller_id=env.get("DEVICE_ID", "unknown"),
        auth_mode=mode,
        token=token,
    )
