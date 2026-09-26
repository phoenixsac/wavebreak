# Wavebreak agent: status (2026-09-26, usage limit reached)

## Done
- Merged master (real lab controller, dashboard, hawkBit UI, demo runbook). hawkBit UI is at http://localhost:8081.
- Fleet MCP server (`agent/fleet_mcp/`): ledger, planner, verdict rules, preconditions, backends (hawkBit, Prometheus+Loki, real lab client, mock lab, bundle diff), `Fleet` service, MCP server with 11 tools. 349 tests pass (`.venv/bin/pytest agent -q`), ruff clean.
- Approval tools `start_wave`, `halt_rollout`, `rollback` are annotated destructive; `agent/register.py` lists them in `require_approval_for_tools`.
- Agent files: `agent/instructions.md`, `agent/wavebreak-agent.json`, `agent/register.py`, `agent/driver.py` (429 backoff, y/n approvals).
- Demo baseline needed no `make demo-reset`: 4 field devices on v1.1, healthy, no rollouts, no lab devices.
- Real-lab rehearse smoke with 1 lab device (`agent/smoke/rehearse_real.py`):
  - v1.3 rev B, 90 s: fail (2 restarts), plan BLOCKED, lab device destroyed. Works as expected.
  - v1.2 rev B, 90 s: **fail** (memory slope 11 MB/min), not a pass. The leak shows in the lab within 90 s, so design §20 step 2 (short rehearsal passes v1.2) does not hold with the real lab; the demo story needs revisiting.
- Memory: 4 field + 1 lab device peaked at 12 containers; about 1 GB available throughout; a lab device costs about 50 MB. Devices exceeded your budget of 4 total (4 field devices were already running).

## Not done
- Start the fleet MCP server, register it in TrueForge, register the agent (`python3 agent/register.py`).
- End-to-end dry run via `agent/driver.py` ("v1.3 is published, take care of the rollout").
- Uncommitted before this commit: nothing else.

## Notes
- Lab create can take longer than 30 s; the lab client timeout is now 120 s. A timed-out create leaves an orphan lab device (I deleted lab-001 by hand); a cleanup for that is not implemented.
- Run the server with the lab token from `~/wavebreak/.env`: `LAB_API_TOKEN`.
