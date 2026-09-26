"""Drive the Wavebreak agent over the TrueForge HTTP API (SSE), with human approvals and 429 backoff.

Usage:
  python3 agent/driver.py "v1.2 is published. Take care of the rollout."
  python3 agent/driver.py --session <id> "continue"
Approval-gated calls (start_wave, halt_rollout, rollback) pause the turn; the driver shows the tool
call and asks y/n on stdin. Provider rate limits (HTTP 429 in a failed turn) are retried with
backoff by sending "continue" to the same session. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

MAX_RETRIES = 6


def api(base: str, method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base}/api/v1{path}", data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def stream_turn(base: str, session_id: str, items: list[dict], timeout: float = 1800):
    """Yield SSE events (dicts) of a turn until the stream closes."""
    req = urllib.request.Request(
        f"{base}/api/v1/sessions/{session_id}/turns",
        data=json.dumps({"input": items, "stream": True}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("data:"):
                try:
                    yield json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue


def retry_delay(message: str, attempt: int) -> float:
    """Seconds to wait after a rate-limit error: the provider hint if present, else exponential."""
    hint = re.search(r"retry in ([0-9.]+)s", message or "")
    base = float(hint.group(1)) + 2 if hint else 15 * 2**attempt
    return min(base, 120)


def is_rate_limit(message: str) -> bool:
    text = (message or "").lower()
    return "429" in text or "quota" in text or "rate limit" in text or "resource_exhausted" in text


def run_turn(base: str, session_id: str, items: list[dict], out=sys.stdout) -> dict:
    """Run one turn, printing a compact log. Returns the final `turn.done` state plus tool-call info."""
    calls: dict[str, dict] = {}  # tool call id -> {name, args}
    by_index: dict[int, str] = {}
    state: dict = {}
    for ev in stream_turn(base, session_id, items):
        kind = ev.get("type")
        if kind == "model.message.delta":
            for tc in ev.get("tool_calls", []):
                idx = tc.get("index", 0)
                if tc.get("id"):
                    by_index[idx] = tc["id"]
                    calls[tc["id"]] = {"name": tc["function"].get("name", ""), "args": ""}
                cid = by_index.get(idx)
                if cid:
                    calls[cid]["args"] += tc.get("function", {}).get("arguments", "") or ""
            if ev.get("content"):
                out.write(ev["content"])
                out.flush()
        elif kind == "tool.response":
            name = calls.get(ev.get("tool_call_id", ""), {}).get("name", "?")
            print(f"\n[tool {name}] {str(ev.get('content', ''))[:300]}", file=out)
        elif kind == "tool.approval_required":
            print("\n[approval required]", file=out)
        elif kind == "turn.done":
            state = ev.get("state", {})
    state["_calls"] = calls
    return state


def ask_approval(call: dict, auto: bool | None) -> bool:
    print(f"\n>>> APPROVAL NEEDED: {call['name']}({call['args']})")
    if auto is not None:
        print(f">>> {'auto-approved' if auto else 'auto-denied'} (test flag)")
        return auto
    return input(">>> Approve? [y/N] ").strip().lower() in ("y", "yes")


def drive(base: str, session_id: str, prompt: str, auto: bool | None = None) -> None:
    items = [{"type": "user.message", "content": prompt}]
    retries = 0
    while True:
        state = run_turn(base, session_id, items)
        status = state.get("status")
        if status == "error":
            msg = state.get("message", "")
            print(f"\n[turn error] {msg[:300]}")
            if is_rate_limit(msg) and retries < MAX_RETRIES:
                delay = retry_delay(msg, retries)
                retries += 1
                print(f"[rate limited: waiting {delay:.0f}s, retry {retries}/{MAX_RETRIES}]")
                time.sleep(delay)
                items = [{"type": "user.message", "content": "continue"}]
                continue
            return
        retries = 0
        pending = [a for a in state.get("required_actions", []) if a.get("type") == "tool.approval_required"]
        if not pending:
            print("\n[done]")
            return
        items = []
        for action in pending:
            for tc in action["tool_calls"]:
                ok = ask_approval(state["_calls"].get(tc["id"], {"name": "?", "args": ""}), auto)
                items.append(
                    {
                        "type": "user.tool_approval",
                        "thread_id": action["thread_id"],
                        "tool_call_id": tc["id"],
                        "approval": {"status": "allow" if ok else "deny"},
                    }
                )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--base-url", default="http://localhost:8790")
    ap.add_argument("--agent", default="wavebreak")
    ap.add_argument("--session", help="continue an existing session")
    ap.add_argument("--auto-approve", action="store_true", help="TEST ONLY: approve every gated call")
    ap.add_argument("--auto-deny", action="store_true", help="TEST ONLY: deny every gated call")
    args = ap.parse_args()
    auto = True if args.auto_approve else False if args.auto_deny else None
    session = (
        args.session or api(args.base_url, "POST", "/sessions", {"agent": {"name": args.agent}})["data"]["id"]
    )
    print(f"session {session}")
    drive(args.base_url, session, args.prompt, auto)


if __name__ == "__main__":
    main()
