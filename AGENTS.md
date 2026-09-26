# AGENTS.md — Wavebreak Project Context

## 1. Situation

- **Event**: "Agents That Act" hackathon (HackCulture; TrueFoundry + Polaris). Build window: 6–7 hours.
- **Challenge**: an agent for a real-world job. It must reach a real system, run generated code in a sandbox, and stop for human approval before irreversible actions.
- **Stack**: TrueFoundry (hosting, LLM gateway), TrueForge (MIT open-source agent harness from the organizers), AWS (credits provided).
- **Product**: Wavebreak — "catches bad OTA rollouts at runtime and rolls them back, with your approval."

## 2. Domain

Edge AI camera fleets managed from a cloud backend. Devices push telemetry and receive app updates via OTA (Eclipse hawkBit).

| Source on device | Transport | Data |
|------------------|-----------|------|
| inference-app textfile metrics + node_exporter | Fluent Bit → Prometheus remote_write | app memory (cgroup, RSS), fps, frames, restarts, boot time |
| journald, kmsg (Firecracker only), boot.log | Fluent Bit → Loki push | app / ota-agent / systemd logs, OOM kills, boot records |

## 3. Problem

An update installs fine, then fails at runtime (memory leak → OOM kill loop on one hardware revision; config crash loop on every device). hawkBit thresholds only see install failures, so the regression spreads.

## 4. Plan

- **Part A (overnight, now)**: complete production-like environment. Tracker: `docs/IMPLEMENTATION.md`. Instructions: `docs/prompts/overnight.md`.
- **Part B (build day)**: the rollout-manager agent: inventory → rehearse in lab → canary waves 2 → 5 → all → verify → promote, halt, or roll back with human approval. Not built yet.
- Part B agent design: `docs/agent-design.md`.

## 5. Environment Design (summary; details in docs/architecture.md)

- **Zones**: backend (hawkBit, Prometheus, Loki, Grafana, mcp-grafana, hawkBit MCP if released), field (devices, outbound only), lab (isolated rehearsal devices behind the lab controller API).
- **Device**: one image (`sim/device/Dockerfile`), Debian slim + systemd PID 1, runs as a systemd container or a Firecracker microVM. Identity `DEVICE_ID`, `HW_REV` (A or B), `REGION`.
- **Releases**: v1.0 baseline, v1.1 good, v1.2 leak on rev B (F1), v1.3 config crash on all (F2), v1.4 fix. Faults are real code.
- **Profiles**: lite (4 devices, H2) for local; full (20 devices, hawkBit DB) on AWS `m8i.2xlarge`.

## 6. Repo Map

```
AGENTS.md                 # this file
CLAUDE.md                 # points here
README.md                 # human quickstart
Makefile                  # PROFILE=lite or full, RUNTIME=container or firecracker
.claude/agents/           # subagents: scaffolder, implementer, verifier, researcher
docs/
  architecture.md         # design source of truth
  IMPLEMENTATION.md       # task tracker for the overnight loop
  prompts/overnight.md    # loop instructions (verbatim)
  view/generate.py        # Markdown → HTML (index.html git-ignored)
platform/                 # backend compose + configs (prometheus, loki, grafana)
sim/
  device/                 # device image, systemd units, Fluent Bit config
  ota-agent/              # DDI + local-install client (stdlib) + tests
  inference-app/          # app source (shared by bundles) + tests
  bundles/v1.0 … v1.4/    # release contents
  fleet/                  # fleet.yaml + fleetctl launcher
  runtime/firecracker/    # fetch, rootfs build, VM launcher
lab/controller/           # rehearsal lab API (FastAPI) + tests
wavebreak_clients/        # thin hawkBit / observability / lab clients for the agent
infra/aws/                # launch.sh, install.sh (untested until build day)
scripts/                  # build/publish bundles, seed, grafana-sa, overnight.sh
  host/setup-fc-net.sh    # once, with sudo: Firecracker bridges, NAT, taps
agent/                    # Part B (empty)
logs/, build/, run/       # git-ignored
```

## 7. How to Run

```bash
cp .env.example .env
make up PROFILE=lite                  # backend
make bundles publish                  # build + upload releases to hawkBit
make fleet seed RUNTIME=container     # devices, then assign v1.0
make lab                              # rehearsal lab controller
make view                             # docs viewer
make down
```

## 8. Local Environment (verified 2026-09-25, run 1)

| Item | Value |
|------|-------|
| OS | Ubuntu 24.04.1 on WSL2, kernel 6.18.33.2-microsoft-standard-WSL2 |
| RAM / CPU / disk | 3.5 GB (+1 GB swap), 8 vCPU, 950 GB free |
| Docker / Compose | 29.1.3, cgroup v2, systemd driver / v2.40.3 |
| Python | 3.12.3 (`python3`; no `python` alias). Project venv `.venv` with ruff, pytest, pyyaml |
| Tools | git, jq, curl, make, tmux, shellcheck, fakeroot, mkfs.ext4 1.47 (`-d` supported), iptables, ip |
| KVM | `/dev/kvm` rw for user (kvm group). Nested virt works: Firecracker boots a guest kernel |
| Firecracker | v1.17.0 at `~/.local/bin/firecracker`; kernel `~/.local/share/firecracker/vmlinux` → 6.1.155 (from CI v1.15) |
| systemd container | works unprivileged with `--cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock`; MemoryMax enforced |
| sudo | passwordless |
| Claude CLI | 2.1.283; `-p`, `--model`, `--dangerously-skip-permissions`, `--max-turns` (hidden but accepted) |
| git remote | `origin` https://github.com/phoenixsac/wavebreak.git |

## 9. Subagents and the Overnight Loop

| Subagent | Model | Use for |
|----------|-------|---------|
| scaffolder | haiku | configs, compose, Makefile, provisioning, small scripts, docs formatting |
| implementer | sonnet | feature code + tests from a precise spec |
| verifier | haiku | lint / tests / smoke runs; short pass-fail summary; always tears down |
| researcher | haiku | facts from official docs with URLs |

Main session (opus) orchestrates and owns the hard parts: ota-agent install semantics, hawkBit integration, Firecracker runtime.

Loop: `tmux new -s night` → `./scripts/overnight.sh`. Each run: `claude -p "Continue per docs/prompts/overnight.md"`; stops on `STATUS: COMPLETE`, sleeps 30 min on usage limits, stops after 3 runs without commits. Log: `logs/overnight.log`.

## 10. Working Rules

1. **Docs first**: any design change updates `docs/architecture.md` in the same commit, then `make view`.
2. **Terse over verbose**. Tables and bullets. No marketing language. No `|` inside table cells (generator splits on it).
3. **Never fabricate**: mark unverified facts `TODO(verify)`; verify against official docs or by running things.
4. **Everything reproducible by script**; no unwritten manual steps.
5. **Local first** (docker compose), then AWS via `infra/aws`. Same compose files in both.
6. **No secrets in git**; `.env` with a committed `.env.example`.
7. **No employer code or data**.
8. **Small commits** with clear messages.
9. **Record decisions** in `docs/architecture.md` §9.
10. **RAM budget ~2.5 GB locally**: one heavy component group at a time; tear down after every smoke test; never run the full fleet locally.

## Cost discipline (all tools)
- Default to medium reasoning. Use high reasoning only for: ota-agent install/rollback semantics, Firecracker runtime, hawkBit integration, debugging a failing smoke test.
- Mechanical work (configs, compose, Makefile, docs formatting, renames) should be done quickly without extended reasoning.
- If the tool supports subagents or cheaper models, delegate mechanical and verification tasks to them.
- Read only the files needed for the current task; don't re-read large docs every step.
- One task at a time; commit after each; update docs/IMPLEMENTATION.md.
