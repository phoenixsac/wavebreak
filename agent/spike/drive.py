"""Spike driver: create agent/session, run turns over HTTP+SSE, print events with timestamps."""
import json
import sys
import time

import httpx

BASE = "http://localhost:8790/api/v1"
MODEL = "google-gemini/gemini-3-5-flash-lite"
T0 = time.time()


def ts() -> str:
    return f"[{time.time() - T0:6.1f}s]"


def create_agent(name: str, instructions: str, tools_cfg: dict, config: dict | None = None) -> None:
    body = {
        "name": name,
        "description": "spike agent",
        "manifest": {
            "model": {"name": MODEL, "params": {"temperature": 0.1}},
            "instructions": instructions,
            "mcp_servers": [tools_cfg],
            **({"config": config} if config else {}),
        },
    }
    r = httpx.delete(f"{BASE}/agents/{name}")
    r = httpx.post(f"{BASE}/agents", json=body)
    print(ts(), "create agent", r.status_code, r.text[:300])


def create_session(agent_name: str) -> str:
    r = httpx.post(f"{BASE}/sessions", json={"agent": {"name": agent_name}})
    r.raise_for_status()
    sid = r.json()["data"]["id"]
    print(ts(), "session", sid)
    return sid


def run_turn(sid: str, items: list[dict], timeout: float = 900) -> list[dict]:
    """Stream a turn; return events. Stream ends at turn.done or when it pauses."""
    events = []
    with httpx.stream("POST", f"{BASE}/sessions/{sid}/turns", json={"input": items, "stream": True},
                      timeout=httpx.Timeout(timeout, connect=10)) as r:
        print(ts(), "turn http", r.status_code)
        if r.status_code != 200:
            print(r.read().decode()[:500])
            return events
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            events.append(ev)
            t = ev.get("type")
            if t in ("model.message.delta", "turn.streaming"):
                continue
            print(ts(), t, json.dumps(ev)[:400])
    print(ts(), "stream closed")
    return events


if __name__ == "__main__":
    pass
