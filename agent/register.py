"""Register the wavebreak-fleet and grafana MCP servers in a running TrueForge and upsert the Wavebreak agent.

Usage: python3 agent/register.py [--render-only] [--base-url http://localhost:8790] [--fleet-url http://127.0.0.1:8792/mcp]

Reads agent/instructions.md, renders agent/wavebreak-agent.json (the full agent manifest), and
creates or updates the agent named "wavebreak" through the TrueForge HTTP API. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_NAME = "wavebreak"
MODEL = "google-gemini/gemini-3-6-flash"
APPROVAL_TOOLS = [
    "start_wave",
    "halt_rollout",
    "rollback",
]  # keep in sync with fleet_mcp.server.APPROVAL_TOOLS
GRAFANA_TOOLS = [
    "query_prometheus",
    "query_loki_logs",
    "list_datasources",
    "list_prometheus_metric_names",
    "list_loki_label_values",
    "search_dashboards",
    "get_dashboard_summary",
]


def model_params(model: str) -> dict:
    """Lite models reject `reasoning_effort` (TrueForge answers 422)."""
    params: dict = {"temperature": 0.1}
    if "lite" not in model:
        params["reasoning_effort"] = "medium"
    return params


def manifest(model: str = MODEL) -> dict:
    """The agent spec (docs/agent-design.md section 14)."""
    return {
        "model": {"name": model, "params": model_params(model)},
        "instructions": (HERE / "instructions.md").read_text(),
        "mcp_servers": [
            {
                "name": "wavebreak-fleet",
                "enable_tools": ["@all"],
                "require_approval_for_tools": APPROVAL_TOOLS,
                "preload": True,
            },
            {
                "name": "grafana",
                "enable_tools": GRAFANA_TOOLS,
                "preload": False,
            },
        ],
        "config": {
            "sandbox": {"enabled": True},
            "generative_ui": {"enabled": True},
            "ask_user_questions": {"enabled": True},
            "dynamic_sub_agents": {"enabled": True},
            "context_management": {
                "compaction": {"enabled": True},
                "large_tool_response": {"enabled": True},
            },
            "iteration_limit": 300,
        },
        "messages": [
            {
                "type": "user.message",
                "content": "Start by calling get_rollout_state and get_fleet_inventory.",
            }
        ],
    }


def call(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base}/api/v1{path}", data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": {"message": raw.decode(errors="replace")}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8790")
    ap.add_argument("--fleet-url", default="http://127.0.0.1:8792/mcp")
    ap.add_argument("--grafana-url", default="http://127.0.0.1:8000/mcp")
    ap.add_argument("--model", default=MODEL, help="TrueForge model id (default: the demo model)")
    ap.add_argument("--render-only", action="store_true", help="only write wavebreak-agent.json")
    args = ap.parse_args()

    spec = manifest(args.model)
    (HERE / "wavebreak-agent.json").write_text(json.dumps(spec, indent=2) + "\n")
    print("wrote agent/wavebreak-agent.json")
    if args.render_only:
        return

    servers = [
        (
            "wavebreak-fleet",
            args.fleet_url,
            "Wavebreak rollout tools: inventory, plan, rehearse, waves, observe, halt, rollback.",
        ),
        ("grafana", args.grafana_url, "Read-only Grafana: PromQL and LogQL queries, dashboards."),
    ]
    for name, url, description in servers:
        status, out = call(
            args.base_url,
            "PUT",
            "/settings/mcp-servers",
            {"manifest": {"type": "remote", "name": name, "url": url, "description": description}},
        )
        print(f"mcp server {name}: HTTP {status}", "" if status < 300 else out)

    status, out = call(args.base_url, "GET", "/agents")
    existing = {a["name"]: a["id"] for a in out.get("data", [])} if status == 200 else {}
    body = {"name": AGENT_NAME, "description": "Wavebreak rollout manager", "manifest": spec}
    if AGENT_NAME in existing:
        update = {k: v for k, v in body.items() if k != "name"}  # the update endpoint rejects "name"
        status, out = call(args.base_url, "PUT", f"/agents/{existing[AGENT_NAME]}", update)
        print("agent updated:", status, "" if status < 300 else out)
    else:
        status, out = call(args.base_url, "POST", "/agents", body)
        print("agent created:", status, "" if status < 300 else out)


if __name__ == "__main__":
    main()
