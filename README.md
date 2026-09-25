# Wavebreak — Edge Fleet Triage Agent

Autonomous agent that detects firmware regressions across edge AI device fleets, correlates with rollout history, and orchestrates rollbacks via hawkBit — with human approval before irreversible actions.

Built for the "Agents That Act" hackathon (HackCulture / Polaris + TrueFoundry).

## Quick start

```bash
# Prerequisites: Docker, Docker Compose, Python 3.11+, make
cp .env.example .env

make up        # start control plane (hawkBit, Prometheus, Loki, Grafana)
make fleet     # spawn simulated device containers
make view      # regenerate and open docs viewer
make down      # tear down everything
make reset     # down + prune volumes
```

## Repo layout

| Path | Purpose |
|------|---------|
| `docs/architecture.md` | Design source of truth |
| `docs/view/` | HTML docs viewer (generated) |
| `platform/` | Docker Compose for control plane |
| `sim/device/` | Simulated edge device image |
| `sim/ota-agent/` | hawkBit DDI client |
| `sim/firmware/` | Firmware bundles (v2.2, v2.3) |
| `infra/aws/` | EC2 bootstrap script |
| `agent/` | Triage agent (Part B) |
| `AGENTS.md` | Full project context for AI agents |

## Docs

All design lives in `docs/architecture.md`. Run `make view` to generate the interactive HTML viewer with presentation mode (press `P`).
