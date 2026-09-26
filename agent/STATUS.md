# Wavebreak agent: status (2026-09-26)

## Done
- **Fleet MCP server** (`agent/fleet_mcp/`, 11 tools, 354 tests pass, ruff clean) is running on :8792 with the REAL lab controller, hawkBit, Prometheus, Loki. Start: `agent/start_fleet_mcp.sh` (log `run/fleet-mcp.log`, ledger `run/fleet-ledger.sqlite`); currently started with `FLEET_LAB_PARALLEL=1` (one lab device at a time).
- **Registered in TrueForge** (http://localhost:8790): MCP servers `wavebreak-fleet` and `grafana` (7 read-only tools), agent `wavebreak` (`agent/register.py`, spec in `agent/wavebreak-agent.json`, instructions in `agent/instructions.md`). `start_wave`, `halt_rollout`, `rollback` are annotated destructive and listed in `require_approval_for_tools`.
- **Real-lab rehearse smoke, 1 lab device:** v1.3 rev B fails (2 restarts); v1.2 rev B fails (memory slope 11 MB/min within 90 s).
- **Dry run via `agent/driver.py`** (fleet on v1.1, "v1.3 is published. Take care of the rollout."): inventory, plan (wave 1 = edge-002 A + edge-003 B), rehearse on real lab devices for both revs: verdict fail (A: 3 restarts, B: 2 restarts), plan BLOCKED, decision recorded (ledger event 3), bundle diff used for root cause (renamed config key, KeyError on first reload; matches the v1.3 source), final report written. No approval prompts, no field device touched, lab devices cleaned up (none left). Took about 6 min (Gemini 3.6 flash, medium reasoning).
- Fleet ends on 4 x v1.1, all healthy (edge-002 showed one transient unhealthy reading, active with 0 restarts and healthy on recheck; nothing in my flow writes to field devices before an approved wave).
- Memory: 4 field + 1 lab device at a time; about 1.1 GB available throughout. Total devices went above your 4 (4 field devices were already running).

## Findings that affect the demo
- **v1.2 does NOT pass a short rehearsal on the real lab.** The leak shows as an 11 MB/min slope within 90 s, so design §20 step 2 (short rehearsal passes v1.2, then canary catches it) does not hold. Options: make the rehearsal window shorter than the slope needs (`rehearsal_slope` threshold or 45 s window), rehearse rev A only, or change the story (rehearsal catches v1.2 too, v1.4 rolls out clean). Needs your decision.
- Lab device create can take more than 30 s; lab client timeout is now 120 s. A create that times out leaves an orphan lab device (none currently; automatic cleanup not implemented).

## Not done
- Approval path end to end with a human (start_wave, halt, rollback) on a field regression; needs a scenario that passes rehearsal (see above).
- Full v1.4 clean rollout (2 -> 5 -> all).
