# Wavebreak

**An agent that catches bad OTA firmware rollouts at runtime, holds or rolls them back, and asks a human before anything irreversible.**

Built for the "Agents That Act" hackathon (HackCulture, TrueFoundry, Polaris).

## The problem

An OTA update to an edge AI fleet "succeeds" when it installs. Real regressions show up *after* install: a memory leak that only hits one hardware revision, a config change that crashes every device 45 seconds after the health check. The OTA server only counts install failures, so the bad release keeps spreading while people correlate metrics, logs and rollout history by hand.

## What Wavebreak does

1. **Rehearse.** Install the release on throwaway lab devices, one per hardware revision, and watch memory, restarts and OOM kills. A failing revision blocks the release before any field device is touched.
2. **Roll out in stratified waves.** Each wave is its own hawkBit rollout, so success in one wave can never auto-start the next. Wave 1 mixes hardware revisions.
3. **Evidence-gated, approval-gated actions.** `start_wave`, `halt_rollout` and `rollback` need fresh evidence of the right kind and a human Allow. The approval prompt shows the evidence (verdict per revision, numbers, cause).
4. **Partial rollout, hold, rollback.** If only one revision fails rehearsal, the agent ships the healthy revision and holds the other, with the failed evidence recorded in an audit ledger. On a field regression it halts, rolls back the affected cohort and verifies recovery. Missing or unreachable data never counts as healthy: verdicts fail closed to `inconclusive`.

## How it meets the three requirements

| Requirement | How |
|---|---|
| Reach a real system | Real Eclipse hawkBit (rollouts, DDI), Prometheus, Loki, Grafana, and a rehearsal lab controller running throwaway systemd devices. Only the hardware and the firmware payload are simulated; faults are real code (leak, config crash), never fabricated logs |
| Run generated code in a sandbox | TrueForge's local sandbox (bubblewrap; `socat`, `ripgrep`) is enabled for Code Mode and skills. Sandboxed code cannot reach devices or call approval-gated tools. The scripted scenes are driven by MCP tools, so the sandbox is available but not the star of the demo |
| Stop for human approval | Enforced by the TrueForge harness (`require_approval_for_tools`), plus destructive annotations, plus server-side evidence gates that refuse stale, wrong-kind, wrong-plan or uncovered evidence regardless of what the model says |

## Stack

TrueForge (agent harness, MCP, sandbox) · TrueFoundry AI Gateway with `openai-polaris/gpt-4o` (Gemini is the documented fallback) · custom fleet MCP server (11 tools, SQLite ledger) · Eclipse hawkBit · Prometheus · Loki · Grafana · Docker · AWS EC2 (`m8i.2xlarge`).

## Architecture

Diagrams (C4 context, containers, components, sequence flows for the three demo scenes, fault catalogue, state machine): run `make view`, open `docs/view/index.html`, press `P` for slides. Source: `docs/architecture.md`; the agent deep dive is `docs/agent-design.md`.

## Run it

Local (lite: 4 devices, about 3.5 GB RAM; put the gateway values `TFY_BASE_URL`, `TFY_API_KEY`, `TFY_MODEL` in git-ignored `agent/spike/.env`):

```bash
cp .env.example .env && make up PROFILE=lite && make bundles publish && make grafana-sa
make demo-reset            # 4 devices on v1.1, healthy
python3 -m venv .venv && .venv/bin/pip install -r agent/requirements.txt
agent/spike/start_trueforge.sh && agent/start_fleet_mcp.sh && .venv/bin/python agent/register.py --register-provider
# chat at http://localhost:8790, dashboard at http://localhost:3000
```

AWS (full: 20 devices): `make aws-sync`, then run `infra/aws/install.sh` on the instance. Steps, access and re-registration: [`docs/aws-deploy.md`](docs/aws-deploy.md). The live demo script is [`docs/DEMO.md`](docs/DEMO.md).

## Honest limitations

- **Devices are simulated** (systemd containers with real cgroup limits, real telemetry). Firecracker microVMs are not implemented.
- **Model flakiness.** Some models end their turn after proposing an action. If that happens, type `proceed`; the agent must then call the tool. Only one clean `gpt-4o` run of Scene 2 exists; Scenes 1 and 3 were verified on other models.
- **The rollback path** (halt, rollback, verify recovery) is unit-tested against mock backends only, not run on a live fleet.
- Rehearsal covers only its window (about 2 minutes); faults with later onset are not caught there. Live scenes take 6 to 11 minutes with one lab device at a time.
- The fleet MCP server has no auth and trusts the harness's approval (localhost only). Known drift items are listed in `docs/architecture.md` section 12.

## Repo map

`agent/` fleet MCP server, driver, registration, instructions · `sim/` device image, ota-agent, releases v1.0 to v1.4 · `lab/controller/` rehearsal lab · `platform/` compose stack · `infra/aws/` installer · `scripts/` build, seed, demo reset · `docs/` architecture, agent design, demo script. Project rules for AI tools: `AGENTS.md`.
