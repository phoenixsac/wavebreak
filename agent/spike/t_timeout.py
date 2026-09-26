import sys
from drive import *

secs = int(sys.argv[1])
name = f"spike-wait-{secs}-{int(time.time())}"
create_agent(
    name,
    "You are a test agent. Do exactly what the user asks. Be brief.",
    {"name": "spike", "enable_tools": ["@all"], "preload": True},
)
sid = create_session(name)
evs = run_turn(
    sid,
    [
        {
            "type": "user.message",
            "content": f"Call wait_seconds with seconds={secs}, then say the exact text the tool returned.",
        }
    ],
    timeout=900,
)
for e in evs:
    if e["type"] == "turn.done":
        print("FINAL", json.dumps(e["state"])[:400])
