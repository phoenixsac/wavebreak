"""Register the wavebreak-fleet and grafana MCP servers in a running TrueForge and upsert the Wavebreak agent.

Usage: python3 agent/register.py [--render-only] [--base-url http://localhost:8790] [--fleet-url http://127.0.0.1:8792/mcp]
                                 [--provider openai|google-gemini] [--model <provider>/<model name>] [--register-provider]

Model choice (same script locally and on AWS): `--provider` / env `WAVEBREAK_PROVIDER` picks the provider
(default `google-gemini`, the documented fallback; `openai` is the preferred demo provider), `--model` /
env `WAVEBREAK_MODEL` overrides the provider default. `--register-provider` first registers the provider in
TrueForge (`PUT /api/v1/settings/model-providers`) with the key from env `OPENAI_API_KEY` / `GEMINI_API_KEY`
or from `agent/spike/.env` and the model list from TrueForge's own catalog; the key is never printed.

Reads agent/instructions.md, renders agent/wavebreak-agent.json (the full agent manifest), and
creates or updates the agent named "wavebreak" through the TrueForge HTTP API. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_NAME = "wavebreak"
MODEL = "google-gemini/gemini-3-6-flash"  # default (fallback) model
PROVIDERS = {
    # provider type -> default model (`<provider>/<configured model name>`), env var holding its key
    "google-gemini": {"model": MODEL, "key_env": "GEMINI_API_KEY"},
    "openai": {"model": "openai/gpt-5-5", "key_env": "OPENAI_API_KEY"},
}
ENV_FILE = HERE / "spike" / ".env"
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
    """Model parameters. Gemini lite models reject `reasoning_effort` (TrueForge answers 422).

    OpenAI reasoning models get only `reasoning_effort` (no temperature: they usually accept the default only).
    """
    if model.startswith("openai/"):
        return {"reasoning_effort": "medium"}
    params: dict = {"temperature": 0.1}
    if "lite" not in model:
        params["reasoning_effort"] = "medium"
    return params


def resolve_model(provider: str, model: str | None) -> str:
    """Explicit model, else env WAVEBREAK_MODEL, else the provider's default."""
    if provider not in PROVIDERS:
        raise SystemExit(f"unknown provider {provider!r}; choose one of {', '.join(PROVIDERS)}")
    return model or os.environ.get("WAVEBREAK_MODEL") or PROVIDERS[provider]["model"]


def read_key(provider: str, env: dict[str, str] | None = None, env_file: Path = ENV_FILE) -> str | None:
    """API key of a provider from the environment or agent/spike/.env (never printed)."""
    name = PROVIDERS[provider]["key_env"]
    env = os.environ if env is None else env
    if env.get(name):
        return env[name]
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            key, _, value = line.strip().partition("=")
            if key == name and value.strip():
                return value.strip().strip("'\"")
    return None


def provider_manifest(provider: str, api_key: str, catalog: list[dict]) -> dict:
    """Body of `PUT /settings/model-providers`; models come from TrueForge's catalog for that provider type."""
    entry = next((c for c in catalog if c.get("type") == provider), None)
    if entry is None:
        raise SystemExit(f"TrueForge's catalog has no provider type {provider!r}")
    return {
        "manifest": {
            "type": provider,
            "name": provider,
            "auth": {"api_key": api_key},
            "models": entry["models"],
        }
    }


def redact(body: dict, secret: str) -> str:
    """JSON of a request body with the secret replaced (for logs and tests)."""
    return json.dumps(body).replace(secret, "<hidden>")


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
    ap.add_argument("--provider", default=os.environ.get("WAVEBREAK_PROVIDER", "google-gemini"))
    ap.add_argument(
        "--model", default=None, help="TrueForge model id, <provider>/<model name> (default per provider)"
    )
    ap.add_argument(
        "--register-provider",
        action="store_true",
        help="register the provider (key from env or agent/spike/.env)",
    )
    ap.add_argument("--render-only", action="store_true", help="only write wavebreak-agent.json")
    args = ap.parse_args()

    model = resolve_model(args.provider, args.model)
    if not model.startswith(args.provider + "/"):
        raise SystemExit(f"model {model!r} does not belong to provider {args.provider!r}")
    spec = manifest(model)
    print(f"provider {args.provider}, model {model}")
    (HERE / "wavebreak-agent.json").write_text(json.dumps(spec, indent=2) + "\n")
    print("wrote agent/wavebreak-agent.json")
    if args.render_only:
        return

    if args.register_provider:
        key = read_key(args.provider)
        if not key:
            raise SystemExit(
                f"no API key for {args.provider}: set {PROVIDERS[args.provider]['key_env']} in the environment or in agent/spike/.env"
            )
        status, catalog = call(args.base_url, "GET", "/catalogs/model-providers")
        if status >= 300:
            raise SystemExit(f"cannot read TrueForge's provider catalog: HTTP {status}")
        body = provider_manifest(args.provider, key, catalog.get("data", []))
        status, out = call(args.base_url, "PUT", "/settings/model-providers", body)
        print(f"model provider {args.provider}: HTTP {status}", "" if status < 300 else redact(out, key))
        if status >= 300:
            raise SystemExit(1)

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
