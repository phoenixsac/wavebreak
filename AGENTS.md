# AGENTS.md — Wavebreak Project Context

## 1. Situation

- **Event**: "Agents That Act" hackathon (HackCulture, Polaris + TrueFoundry). Build window: 6–7 hours.
- **Challenge**: build an agent for a real-world job. Must reach a real system, run generated code in a sandbox, and stop before irreversible actions for human approval.
- **Stack**: TrueFoundry (hosting / LLM gateway), TrueForge (open-source agent harness), AWS (credits provided).

## 2. Domain

Edge AI device fleets (e.g. smart cameras) managed from a cloud backend. Devices ship telemetry and receive firmware via OTA.

**Telemetry sources per device:**
| Source | Transport | Data |
|--------|-----------|------|
| node_exporter + textfile collector | Prometheus scrape | CPU, memory, disk, network, temperature, inference FPS, GPU memory, app memory |
| Fluent Bit | Loki push | kmsg, syslog, journald logs |
| Fluent Bit (boot parser) | Loki push | Boot times, crash-dump records, availability |

**OTA**: Eclipse hawkBit — update server with Management API, DDI API, rollouts with groups and thresholds.

## 3. Problem

A firmware rollout "succeeds" (install OK) then fails at runtime (memory leak → OOM kill loops, reboot loops, FPS drop), often on a device subset (hardware revision, region). hawkBit rollout thresholds only see install failures, so the regression spreads. Humans spend hours correlating metrics, logs, and rollout history.

## 4. Agent (Part B — tomorrow)

**Edge Fleet Triage Agent**: detect misbehaving cohort → correlate with changes (firmware, HW rev, region, config) → reproduce in sandbox → propose action with blast radius → wait for human approval → execute via hawkBit → verify recovery.

Placeholder: see `agent/` directory.

## 5. Environment Design

- Control plane and protocols are real; only device hardware and firmware payload are simulated.
- **Simulated device** = Debian container with systemd (PID 1), node_exporter, Fluent Bit, `inference-app` (systemd unit), `ota-agent` (Python DDI client). Identity via env: `DEVICE_ID`, `HW_REV`, `REGION`.
- **Firmware** = versioned tarball uploaded to hawkBit. v2.2 healthy; v2.3 has memory leak triggered when `HW_REV=B`.
- **Control plane**: hawkBit, Prometheus, Loki, Grafana via docker compose.
- **Scale**: 15–30 simulated devices on one host.

## 6. Repo Map

```
AGENTS.md              # this file
CLAUDE.md              # points here
README.md              # human quickstart
Makefile               # view, up, down, fleet, reset
docs/
  architecture.md      # design source of truth
  view/
    generate.py        # Markdown → HTML generator
    index.html         # generated (git-ignored)
platform/              # compose for hawkBit, Prometheus, Loki, Grafana
sim/
  device/              # device container image
  ota-agent/           # hawkBit DDI client
  firmware/            # firmware bundles v2.2, v2.3
infra/
  aws/                 # EC2 bootstrap script
agent/                 # Part B (empty)
scripts/               # utility scripts
```

## 7. How to Run

```bash
cp .env.example .env
make up                # start control plane
make fleet             # spawn devices
make view              # regenerate docs viewer
make down              # tear down
make reset             # down + prune volumes
```

## 8. Environment (local machine)

| Tool | Status | Version |
|------|--------|---------|
| Docker | available | 27.4.0 |
| Docker Compose | available | v2.31.0 |
| Python | available | 3.11.7 |
| make | **not found** | install via `choco install make` or use msys2 |
| aws cli | **not found** | install before AWS deploy |

## 9. Working Rules

1. **Docs first**: any design change updates `docs/architecture.md` in the same commit, then `make view`.
2. **Terse over verbose**. Tables and bullets. No marketing language.
3. **Never fabricate**: mark unverified facts as `TODO(verify)`; verify against official docs or by running things.
4. **Everything reproducible by script**; no manual steps that aren't written down.
5. **Local first** (docker compose), then AWS via `infra/aws` bootstrap. Same compose files in both.
6. **No secrets in git**; use `.env` with a committed `.env.example`.
7. **No employer code or data**; everything is original or open source.
8. **Small commits** with clear messages so overnight changes are reviewable.
9. **Record decisions** in `docs/architecture.md` § Decisions Log.
