from drive import *

name = f"spike-codemode-{int(time.time())}"
body = {
    "name": name,
    "description": "code mode approval test",
    "manifest": {
        "model": {"name": MODEL, "params": {"temperature": 0}},
        "instructions": "You are a test agent. Do exactly what the user asks and report raw outputs briefly.",
        "mcp_servers": [
            {
                "name": "spike",
                "enable_tools": ["@all"],
                "require_approval_for_tools": ["write_marker"],
                "preload": False,
            }
        ],
        "config": {"sandbox": {"enabled": True}},
    },
}
r = httpx.post(f"{BASE}/agents", json=body)
print(ts(), "agent", r.status_code)
sid = create_session(name)
msg = (
    "Use the sandbox exec tool (shell) and run exactly these two commands, in separate exec calls, and report each raw output:\n"
    "1) mcp-client call-tool spike get_status '{}'\n"
    '2) mcp-client call-tool spike write_marker \'{"text":"codemode-bypass"}\''
)
evs = run_turn(sid, [{"type": "user.message", "content": msg}], timeout=300)
json.dump({"sid": sid, "events": evs}, open("codemode_events.json", "w"))
for e in evs:
    if e["type"] == "tool.response":
        print("TOOLRESP", e["content"][:700])
    if e["type"] == "turn.done":
        print("FINAL", json.dumps(e["state"])[:900])
print("markers:", open("spike_markers.txt").read().split())
