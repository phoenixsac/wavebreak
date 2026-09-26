from drive import *
name = f"spike-skill-{int(time.time())}"
body = {"name": name, "description": "skill test", "manifest": {
    "model": {"name": MODEL, "params": {"temperature": 0}},
    "instructions": "You are a test agent. Be brief.",
    "skills": [{"name": "spike-pdf"}],
    "config": {"sandbox": {"enabled": True}}}}
r = httpx.post(f"{BASE}/agents", json=body); print(ts(), "agent", r.status_code, r.text[:200])
sid = create_session(name)
evs = run_turn(sid, [{"type": "user.message", "content": "Load your pdf skill and tell me the first line after the frontmatter (the first heading) of its SKILL.md, and which files the skill directory contains."}], timeout=300)
for e in evs:
    if e["type"] == "tool.response": print("TOOLRESP", e["content"][:500])
    if e["type"] == "turn.done": print("FINAL", json.dumps(e["state"])[:700])
