from drive import *

create_agent(
    "spike-approval",
    "You are a test agent. Use the tools exactly as the user asks. Be brief.",
    {
        "name": "spike",
        "enable_tools": ["@all"],
        "require_approval_for_tools": ["write_marker"],
        "preload": True,
    },
)
sid = create_session("spike-approval")
evs = run_turn(
    sid,
    [
        {
            "type": "user.message",
            "content": "First call get_status. Then call write_marker with text 'hello-approval'. Then tell me what happened.",
        }
    ],
)
json.dump({"sid": sid, "events": evs}, open("approval_events.json", "w"))
print("markers file exists:", __import__("os").path.exists("spike_markers.txt"))
