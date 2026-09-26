# Wavebreak agent: status (2026-09-26, end of steps 1 to 5)

## What passed
- **Step 1 housekeeping:** AGENTS.md describes one branch (`master`); `agent/requirements.txt` (`mcp>=1.2,<2`, `pydantic`) plus `requirements-dev.txt`, installed in `.venv`; start scripts executable, use `.venv/bin/python`, create `run/`, refuse a second copy (verified against the running servers); `infra/aws/install.sh` installs the requirements, starts TrueForge and the fleet MCP (`OUTBOUND_URL_ALLOWED_HOSTS`, `MCP_REQUEST_TIMEOUT_MS=900000`, socat, ripgrep) and registers the agent (static check only, not run on AWS).
- **Step 2 partial rollout:** `plan_rollout(exclude_hw_revs, exclusion_evidence_id)`; the hold needs failed-rehearsal evidence of the same version for every held hw_rev; new plan field `held`, ledger event `hold`, `get_rollout_state.held`, held devices in `excluded` with the evidence id. 9 new tests, **364 pass**, ruff clean on `agent/fleet_mcp`, `driver.py`, `register.py`. Instructions updated (partial rollout rule, report lists held cohorts, "a named release is the whole request").
- **Step 3 scenes, run through TrueForge with the driver (`--auto-approve`, every grant logged in `run/scenes/p1/scene*.log` and the ledger), one lab device at a time, hawkBit UI stopped:**

| Scene | Result | Wall time |
|---|---|---|
| 3 v1.4 | 2 separate hawkBit rollouts (2 targets each, each after its own approval), 4 of 4 on v1.4, healthy | 10 min 6 s |
| 2 v1.2 | rehearsal A pass, B fail (slope 11.3 MB/min); partial plan rev A, `hold` event cites the failed evidence; 2 approved waves; rev A on v1.2 healthy, rev B (edge-001, edge-003) still v1.1 | 15 min 3 s |
| 1 v1.3 | rehearsal fails on both revs (restarts), plan BLOCKED, no rollout created, fleet still v1.1 healthy, lab empty | 6 min 2 s |

  Memory during the runs: never more than 4 field + 1 lab devices, at least 1.2 GB available. Approvals granted: Scene 3 two (`start_wave` 1 and 2), Scene 2 two, Scene 1 none.
- **Step 4:** `docs/agent-design.md` §20 (final storyline, prompts, expected output per step, timings), §23 (A18 to A23), plus §5, §9, §10, §10.1, §11, §14, §15, §17, §21, §22 brought in line with the code.
- **Step 5:** `docs/architecture.md` restructured to sections 0 to 12 (journey, C4 L1 to L3, 8 flows including the three scenes, interfaces, data, fault catalogue, state machine, runbook, decisions, open questions, drift report); 16 Mermaid diagrams all pass `mermaid.parse`; TOC anchors all resolve; `make view` regenerated; read mode and present mode (P, arrows, Esc, 15 slides) exercised headlessly in jsdom with mermaid stubbed. Visual rendering was NOT checked (no browser here): open `docs/view/index.html` once. The §3 and §4 slides hold 5 and 8 diagrams and will scroll in present mode.

## Timings versus the 8-minute target
Not met with one lab device at a time (see table). Cost is lab rehearsal (5.5 min for two revs serial) plus 2 min soak per wave. Earlier runs with two lab devices at once were faster (Scene 3 about 9 min, Scene 1 3.6 min) but break your 4 field + 1 lab rule. Levers are in `docs/agent-design.md` §20.

## Flakiness and things that went wrong
- **I ran Scenes 1 to 3 first with lab parallelism 2** because I restarted the fleet MCP without `FLEET_LAB_PARALLEL=1`. Fixed (`start_fleet_mcp.sh` now defaults to 1; the AWS installer sets 2) and all three scenes were re-run at 1 (the table above).
- **Gemini free tier: 20 requests per model per day** (observed). `gemini-3-6-flash` was exhausted during my first Scene 2 attempt, so Scenes 1 and 2 (and the final Scene 3) ran on `gemini-3-5-flash-lite`. Only `gemini-3-6-flash` and `gemini-3-5-flash-lite` are configured in TrueForge; the lite model rejects `reasoning_effort`. Scene 3 on the demo model passed earlier the same day.
- One `Cannot connect to API` ended a turn mid-rollout; the driver now retries it (and 429/503) with backoff.
- The lite model stopped once to ask "shall I proceed?"; fixed with an explicit rule in the instructions. Not re-tested on the demo model.
- The agent observed wave 1 twice in the Scene 2 re-run (about 2 min extra); harmless, evidence stayed valid.
- `make demo-reset` took 21 to 88 s. A lab device create can take over 30 s. A create that times out leaves an orphan lab container (no automatic cleanup; none left).
- A stray file named `0` appeared twice in the repo root (source unknown, deleted).
- I ran `git stash` by mistake once and popped it immediately; nothing was lost.

## Drift report summary (full table in `docs/architecture.md` section 12)
Code deviating from agreed intent:
- **High:** C1 rehearsing only some hw_revs of a plan still allows `start_wave` for a wave that includes the others (`preconditions.py:91`); C2 rollback with failed assignments still reaches ROLLED_BACK and `verify_recovery` can hide devices left on the bad version (`fleet.py:764`); C3 the Loki OOM query fails open (`backends.py:309`). Recommendation: fix code.
- **Medium:** C4 evidence freshness is checked after human approval; C5 approval is enforced only by the agent manifest (server has no auth, localhost only); C6 `plan_rollout` holds are ungated and the approval prompt does not show the held cohort; C7 no `rollout-playbook` skill, playbook inline. Recommendation: accept and record (A18 recorded the hold).
- **Low:** C8 to C12 (inconclusive cap only in instructions, orphan lab devices, halt does not cancel pending actions, read-only annotations on evidence writers, serial hawkBit calls).
- Environment: **C-E1 `scripts/demo-reset.sh:101` waits for exactly 4 devices, so `make demo-reset PROFILE=full` (called by the AWS installer) will not complete: fix before AWS.** C-E2 Firecracker runtime is not implemented (docs now say parked). C-E3 lab controller turns a device 422 install failure into 502. C-E4 field network is not least-privilege (Prometheus lifecycle endpoint, Management API reachable from devices). C-E5 to C-E14 low items (unpinned images, dead env vars, `make test` targets, half-done target-token mode).
- Docs were corrected wherever the code was the fact (ports, networks, endpoints, metrics, labels, health check, fault symptoms, Fluent Bit config, ledger layout).

## What you must do by hand
1. **Gemini key:** use a paid key (or wait for the daily reset) before the demo; the agent is registered on the demo model `gemini-3-6-flash` again, which is out of free quota today. Put the key in `agent/spike/.env`.
2. Open `docs/view/index.html` in a browser and check the diagrams and present mode (P) visually.
3. Decide on C-E1 (demo-reset for 20 devices) and C1 to C3 before build day; I did not change them (you asked for a drift report, not fixes).
4. `docker start wavebreak-hawkbit-ui-1` when you want the hawkBit UI back (I stopped it to save memory). TrueForge (:8790) and the fleet MCP (:8792, lab parallel 1) are still running; the lab is empty; the fleet is on v1.1 and healthy.
5. On AWS: run `infra/aws/install.sh` (untested), provide `agent/spike/.env`, and push `master` first (done at the end of this run).
