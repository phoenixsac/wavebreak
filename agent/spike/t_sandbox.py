from drive import *
import pathlib

probe = pathlib.Path("probe.sh").read_text()
name = f"spike-sandbox-{int(time.time())}"
body = {
    "name": name,
    "description": "sandbox probe",
    "manifest": {
        "model": {"name": MODEL, "params": {"temperature": 0}},
        "instructions": "You are a test agent with a sandbox. Run exactly the shell script the user gives, verbatim, in ONE command, and reply with its full raw output. Also list the names of the tools you have.",
        "config": {"sandbox": {"enabled": True}},
    },
}
r = httpx.post(f"{BASE}/agents", json=body)
print(ts(), "agent", r.status_code, r.text[:200])
sid = create_session(name)
evs = run_turn(
    sid,
    [
        {
            "type": "user.message",
            "content": "Run this script verbatim in the sandbox:\n```bash\n" + probe + "\n```",
        }
    ],
    timeout=300,
)
json.dump(evs, open("sandbox_events.json", "w"))
for e in evs:
    if e["type"] == "turn.done":
        print("FINAL", (e["state"].get("output") or {}).get("content", e["state"]))
