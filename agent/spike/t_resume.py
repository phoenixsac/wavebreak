from drive import *
d = json.load(open("approval_events.json"))
sid = d["sid"]
req = [e for e in d["events"] if e["type"] == "tool.approval_required"][0]
tc = req["tool_calls"][0]["id"]
evs = run_turn(sid, [{"type": "user.tool_approval", "thread_id": req["thread_id"], "tool_call_id": tc, "approval": {"status": "allow"}}])
print("markers:", open("spike_markers.txt").read() if __import__("os").path.exists("spike_markers.txt") else None)
