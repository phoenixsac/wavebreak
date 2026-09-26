"""Thin hawkBit Management API client for inventory and explicitly started waves.

Each rollout created by :meth:`create_wave` contains exactly one group and is
left in hawkBit's ready state. Call :meth:`start_rollout` explicitly after the
human approval gate. A later canary wave is a separate rollout and cannot be
started automatically by this rollout's group-success policy.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from wavebreak_clients._http import HttpError, request, request_json


class HawkbitClient:
    """hawkBit Management API client using HTTP Basic authentication."""

    def __init__(self, base_url: str, username: str, password: str, *, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/rest/v1"):
            self.api = self.base_url
        else:
            self.api = f"{self.base_url}/rest/v1"
        self.auth = (username, password)
        self.timeout = timeout

    def list_targets(self, query: str | None = None, *, limit: int = 500) -> list[dict[str, Any]]:
        """List targets, optionally filtered with hawkBit FIQL."""
        result = request_json("GET", f"{self.api}/targets", params={"q": query, "limit": limit},
                              auth=self.auth, timeout=self.timeout)
        return result.get("content", [])

    def target(self, target_id: str) -> dict[str, Any]:
        return request_json("GET", f"{self.api}/targets/{urllib.parse.quote(target_id, safe='')}",
                            auth=self.auth, timeout=self.timeout)

    def target_attributes(self, target_id: str) -> dict[str, Any]:
        return request_json("GET", f"{self.api}/targets/{urllib.parse.quote(target_id, safe='')}/attributes",
                            auth=self.auth, timeout=self.timeout)

    def installed_distribution_set(self, target_id: str) -> dict[str, Any]:
        return request_json("GET", f"{self.api}/targets/{urllib.parse.quote(target_id, safe='')}/installedDS",
                            auth=self.auth, timeout=self.timeout)

    def list_distribution_sets(self, query: str | None = None, *, limit: int = 500) -> list[dict[str, Any]]:
        result = request_json("GET", f"{self.api}/distributionsets",
                              params={"q": query, "limit": limit}, auth=self.auth,
                              timeout=self.timeout)
        return result.get("content", [])

    def distribution_set_id(self, name: str, version: str) -> int:
        """Look up a distribution set by exact name and version."""
        fiql = f"name=={name};version=={version}"
        matches = self.list_distribution_sets(fiql, limit=50)
        exact = [item for item in matches if item.get("name") == name and item.get("version") == version]
        if len(exact) != 1:
            raise LookupError(f"expected one distribution set {name} {version}; found {len(exact)}")
        return int(exact[0]["id"])

    def create_wave(
        self,
        *,
        name: str,
        distribution_set_id: int,
        target_filter_query: str,
        description: str = "",
        weight: int = 400,
    ) -> dict[str, Any]:
        """Create one ready, single-group rollout for a canary wave.

        This method never starts the rollout. Create another rollout for the
        next target wave, and call ``start_rollout`` only after approval.
        """
        body = {
            "name": name,
            "description": description,
            "distributionSetId": int(distribution_set_id),
            "targetFilterQuery": target_filter_query,
            "amountGroups": 1,
            "dynamic": False,
            "type": "forced",
            "weight": int(weight),
            "successCondition": {"condition": "THRESHOLD", "expression": "100"},
            "successAction": {"action": "NEXTGROUP", "expression": ""},
            "errorCondition": {"condition": "THRESHOLD", "expression": "1"},
            "errorAction": {"action": "PAUSE", "expression": ""},
        }
        return request_json("POST", f"{self.api}/rollouts", json_body=body,
                            auth=self.auth, timeout=self.timeout)

    def start_rollout(self, rollout_id: int) -> Any:
        """Explicitly start a previously created rollout."""
        return self._post_empty(f"/rollouts/{int(rollout_id)}/start")

    def pause_rollout(self, rollout_id: int) -> Any:
        return self._post_empty(f"/rollouts/{int(rollout_id)}/pause")

    def resume_rollout(self, rollout_id: int) -> Any:
        return self._post_empty(f"/rollouts/{int(rollout_id)}/resume")

    def stop_rollout(self, rollout_id: int) -> Any:
        return self._post_empty(f"/rollouts/{int(rollout_id)}/stop")

    def rollout(self, rollout_id: int) -> dict[str, Any]:
        return request_json("GET", f"{self.api}/rollouts/{int(rollout_id)}",
                            auth=self.auth, timeout=self.timeout)

    def rollout_groups(self, rollout_id: int) -> list[dict[str, Any]]:
        result = request_json("GET", f"{self.api}/rollouts/{int(rollout_id)}/deploygroups",
                              auth=self.auth, timeout=self.timeout)
        return result.get("content", [])

    def group_targets(self, rollout_id: int, group_id: int) -> list[dict[str, Any]]:
        result = request_json("GET", f"{self.api}/rollouts/{int(rollout_id)}/deploygroups/{int(group_id)}/targets",
                              auth=self.auth, timeout=self.timeout)
        return result.get("content", [])

    def assign_distribution_set(self, target_id: str, distribution_set_id: int,
                                *, action_type: str = "forced") -> Any:
        body = {"id": target_id, "type": action_type}
        return request_json("POST", f"{self.api}/distributionsets/{int(distribution_set_id)}/assignedTargets",
                            json_body=[body], auth=self.auth, timeout=self.timeout)

    def action(self, target_id: str, action_id: int) -> dict[str, Any]:
        return request_json("GET", f"{self.api}/targets/{urllib.parse.quote(target_id, safe='')}/actions/{int(action_id)}",
                            auth=self.auth, timeout=self.timeout)

    def download_artifact(self, module_id: int, artifact_id: int, destination: str | Path) -> Path:
        """Download an artifact to a caller-chosen path."""
        url = f"{self.api}/softwaremodules/{int(module_id)}/artifacts/{int(artifact_id)}/download"
        status, _headers, content = request("GET", url, auth=self.auth, timeout=self.timeout)
        if status >= 400:
            raise HttpError(status, url, content.decode("utf-8", errors="replace"))
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _post_empty(self, path: str) -> Any:
        # hawkBit control endpoints accept an empty POST body.
        status, _headers, content = request("POST", f"{self.api}{path}", auth=self.auth,
                                            timeout=self.timeout)
        if status >= 400:
            raise HttpError(status, f"{self.api}{path}", content.decode("utf-8", errors="replace"))
        return content
