"""Token usage of a TrueForge session, summed from its `model.message` events.

Usage: python3 agent/usage.py <session_id> [--base-url http://localhost:8790]
Prints one JSON object: LLM calls, input / output / cached-input tokens, session cost and duration. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import urllib.request


def get(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}/api/v1{path}", timeout=30) as resp:
        return json.loads(resp.read())


def summarize(events: list[dict], session: dict | None = None) -> dict:
    """Sum `usage` of every `model.message` event (one per LLM call); add session cost and duration."""
    calls = inp = out = cached = 0
    for item in events:
        ev = item.get("event", item)
        usage = ev.get("usage") if ev.get("type") == "model.message" else None
        if usage:
            calls += 1
            inp += int(usage.get("input_tokens") or 0)
            out += int(usage.get("output_tokens") or 0)
            cached += int(usage.get("cache_read_tokens") or 0)
    metrics = (session or {}).get("metrics", {})
    return {
        "llm_calls": calls,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cached,
        "total_tokens": inp + out,
        "cost_usd": metrics.get("total_cost_in_usd"),
        "duration_s": round(metrics["total_duration_ms"] / 1000)
        if metrics.get("total_duration_ms")
        else None,
        "turns": metrics.get("total_turns"),
    }


def paged(base: str, path: str, limit: int) -> list[dict]:
    """All items of a paged list endpoint."""
    items: list[dict] = []
    token = None
    while True:
        page = get(base, f"{path}?limit={limit}" + (f"&page_token={token}" if token else ""))
        items += page.get("data", [])
        token = (page.get("pagination") or {}).get("next_page_token")
        if not token:
            return items


def turn_events(base: str, session_id: str) -> list[dict]:
    """All events of every turn of the session (per-turn endpoint, paged)."""
    events: list[dict] = []
    for turn in paged(base, f"/sessions/{session_id}/turns", 25):
        events += paged(base, f"/sessions/{session_id}/turns/{turn['id']}/events", 100)
    return events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id")
    ap.add_argument("--base-url", default="http://localhost:8790")
    args = ap.parse_args()
    session = get(args.base_url, f"/sessions/{args.session_id}").get("data", {})
    print(json.dumps(summarize(turn_events(args.base_url, args.session_id), session)))


if __name__ == "__main__":
    main()
