from drive import *
# 1) deny path
create_agent("spike-approval", "You are a test agent. Use the tools exactly as the user asks. Be brief.",
             {"name": "spike", "enable_tools": ["@all"], "require_approval_for_tools": ["write_marker"], "preload": True})
sid = create_session("spike-approval")
evs = run_turn(sid, [{"type": "user.message", "content": "Call write_marker with text 'should-be-denied', then report."}])
req = [e for e in evs if e["type"] == "tool.approval_required"][0]
evs = run_turn(sid, [{"type": "user.tool_approval", "thread_id": req["thread_id"], "tool_call_id": req["tool_calls"][0]["id"], "approval": {"status": "deny"}}])
print("=== DENY done; markers:", open("spike_markers.txt").read().split())
# 2) default approval list (no require_approval_for_tools) -> unannotated write should run ungated
create_agent("spike-default", "You are a test agent. Use the tools exactly as the user asks. Be brief.",
             {"name": "spike", "enable_tools": ["@all"], "preload": True})
sid = create_session("spike-default")
evs = run_turn(sid, [{"type": "user.message", "content": "Call write_marker with text 'default-ungated', then report."}])
print("=== DEFAULT done; approval events:", sum(e["type"] == "tool.approval_required" for e in evs), "markers:", open("spike_markers.txt").read().split())
