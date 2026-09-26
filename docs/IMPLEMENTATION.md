# Wavebreak — Implementation Tracker

**Objective**: finish all code and config for the production-like environment (Part A) before the build day, so the build day only goes into the agent. Code is written for everything; verification only where it's cheap (WSL has 3.5 GB RAM; the full system runs on AWS).

- Instructions: [docs/prompts/overnight.md](prompts/overnight.md)
- Design source of truth: [docs/architecture.md](architecture.md)

STATUS: IN PROGRESS

## How to use this file (loop runs)

1. Pick the first task with status `todo` or `in-progress` whose dependencies are all `done` (or `parked` when the task can proceed without them).
2. Mark it `in-progress`, commit. Do it. Verify at the cheapest meaningful level.
3. Mark it `done` / `parked` / `blocked`, fill in **Verif** and **Notes**, add a Run log line, commit.

**Status values**: todo, in-progress, done, parked, blocked.
**Verification levels**: static (lint, config check, dry-run) → unit (pytest) → smoke (ran against real components) → unverified (code only). Add `unverified-local` when the code targets EC2 and cannot run in WSL.
**Owners**: `orch` = main session (opus). Subagents: `scaffolder` (haiku), `implementer` (sonnet), `verifier` (haiku), `researcher` (haiku).
**Table rule**: no pipe characters inside cells (the docs generator splits on them).

## Tasks

### M1 — Docs update

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T1.1 | architecture.md rewritten for the frozen design: three zones, device, releases, lab, Firecracker, sequences, interfaces, fault catalogue, deployment, decisions | orch | — | done | static | Run 1. `make view` renders |
| T1.2 | AGENTS.md: environment findings, repo map, subagents, loop | orch | — | done | static | Run 1 |
| T1.3 | Makefile `view` uses python3 | orch | — | done | static | Run 1 |

### M2 — Backend compose

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T2.1 | Research hawkBit: current image names and tags (`hawkbit/hawkbit-update-server` or split images), official compose DB, env for H2, JVM heap cap, DDI gateway-token config (tenant config API), Management API basic auth, OpenAPI URL, artifact storage location. Record in architecture.md §5 and §10 | researcher | — | done | static | Recorded in architecture.md §5; image inspected; endpoint shapes TODO(verify) in S1 |
| T2.2 | Research hawkBit MCP server: does it exist in a release/image, transport, port, tools. Include in compose if an image exists; else park | researcher | T2.1 | done | static | hawkbit-mcp-server 1.1.0 jar on Maven Central (standalone, streamable HTTP, 8081). Run on hawkBit image JRE, host port 8082. Fetch script + compose service in T2.3 |
| T2.3 | `platform/docker-compose.yml`: networks backend, field, lab (lab `internal: true`); services hawkbit (lite H2, heap cap), prometheus (`--web.enable-remote-write-receiver`), loki, grafana; memory limits; `.env` driven; profile `full` adds hawkBit DB | scaffolder | T2.1 | done | smoke (hawkBit only) | Networks backend, wavebreak_field, wavebreak_lab (internal). Profiles full (MySQL), mcp (jar). hawkBit booted lite (414 MiB), lab user read-only verified (GET 200, POST 403). Image has no curl, no actuator: wait on /v3/api-docs |
| T2.4 | `platform/prometheus/prometheus.yml` (self-scrape only; devices push via remote_write) + `promtool check config` | scaffolder | T2.3 | done | static | promtool check config SUCCESS |
| T2.5 | `platform/loki/config.yaml` (single binary, filesystem, retention small, low memory) | scaffolder | T2.3 | done | static | loki -verify-config rc 0 (3.5.0) |
| T2.6 | Makefile `up` / `down` with PROFILE=lite or full | scaffolder | T2.3 | done | static | Makefile uses --env-file .env; adds mcp profile when jar present |
| T2.7 | hawkBit tenant bootstrap script `scripts/hawkbit-config.sh`: enable gateway-token auth, set token from `.env`, set polling interval | orch | T2.1 | done | smoke | Verified against running server. Min polling 30s by default; lowered with -Dhawkbit.controller.minPollingTime=00:00:05, 10s accepted |

### M3 — Bundles + publish

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T3.1 | `scripts/build-bundles.sh`: tar each `sim/bundles/vX.Y` into `build/bundles/wavebreak-app-vX.Y.tar` + `.sha256`; deterministic tar (sorted, fixed mtime/owner) | scaffolder | T4.2 | done | static | Deterministic (sorted, fixed mtime/owner; rebuild gives same sha). Tar root: manifest.json, config.yaml, app/ |
| T3.2 | `scripts/publish-bundles.sh`: discover SM and DS types via Management API; create SM per version, upload artifact, create DS; idempotent (lookup by name+version) | orch | T2.1, T3.1 | done | smoke | Ran twice against hawkBit 1.1.0: 5 SM + 5 DS created, second run no-op. POST bodies take type KEYS (application, app), not ids |
| T3.3 | Smoke S1: hawkBit alone (H2, heap ~512 MB) + publish + one ota-agent process registers, gets v1.1, installs, reports success; tear down | verifier | T2.7, T3.2, T5.2 | todo | — | See §3 of prompt |

### M4 — inference-app

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T4.1 | inference-app v1.0 (Python stdlib): camera loop, frame size by HW_REV (A 1080p, B 4K, scaled bytes, configurable), JSON log lines with fw_version and labels, textfile metrics every 5s (atomic write) incl. cgroup memory.current, config reload every ~45s, heartbeat metric; pytest | implementer | — | done | unit | inference_app.py stdlib; tests identity, metrics, parser |
| T4.2 | Releases `sim/bundles/v1.0` … `v1.4` (manifest.json, app/, config.yaml) as real code diffs: v1.1 latency metric; v1.2 unbounded 4K enhancement buffer (rev B only, leak pace configurable, OOM ~3 min); v1.3 renamed config key read on first reload → crash; v1.4 bounded buffer. Tests for leak and crash logic | implementer | T4.1 | done | unit | 41 tests incl. test_faults.py: v1.2 unbounded buffer rev B only, v1.3 KeyError on first reload (uncaught, exit 1), v1.4 bounded. Leak ~12 MB/min at defaults (40 KB 4K frames, 5 fps) |

### M5 — ota-agent

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T5.1 | Install core (shared): extract to inactive slot, flip `/opt/app/current`, restart unit, health window (default 15s: unit active + heartbeat advancing), rollback on failure, rewrite labels.env, restart fluent-bit; systemctl/paths injectable for tests; pytest | orch | T4.1 | done | unit | `sim/ota-agent/ota_agent/install.py`; 12 tests (fake systemd + clock): healthy, rollback on crash, no heartbeat + grace, sha mismatch, traversal, labels.env |
| T5.2 | DDI mode: poll with backoff, configData on registration, deploymentBase handling, download + sha256 check against hawkBit hash, feedback proceeding/success/failure, same-version short-circuit, auth flag gateway-token or target-token; pytest against a mock DDI server | orch | T5.1, T2.1 | in-progress | smoke | Live vs hawkBit 1.1.0 (real DdiAgent, fake systemd): register + configData attrs, v1.1 install finished, failing install -> rollback + error feedback, same version -> finished without reinstall. Mock-DDI pytest pending (implementer) |
| T5.3 | Local mode: `ota-agent install <bundle.tar>` CLI + HTTP endpoint (token) POST bundle upload, GET /status; pytest | implementer | T5.1 | in-progress | — | ota_agent/local.py (POST /install raw tar + X-Sha256, GET /status, Bearer token) and __main__.py CLI (ddi, serve, install, status) written; tests pending |

### M6 — Device image

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T6.1 | Research Fluent Bit: exact plugin names and options for prometheus_scrape, prometheus_remote_write, systemd (journald), kmsg, tail, loki (labels from env, record accessor), and dry-run flag; pinned version and Debian install method | researcher | — | done | static | architecture.md §5 Fluent Bit table; v5.1.2 apt repo |
| T6.2 | `sim/device/Dockerfile`: debian bookworm-slim, systemd PID 1 (mask udev/getty), node_exporter (pinned, textfile collector dir), fluent-bit, python3, ota-agent, inference-app v1.0 preinstalled in slot_a, STOPSIGNAL SIGRTMIN+3 | implementer | T4.2, T5.3 | todo | — | Also the Firecracker rootfs source |
| T6.3 | systemd units: inference-app (MemoryMax from profile env, Restart=always), ota-agent (mode from env), node_exporter, fluent-bit, wavebreak-identity (first boot: env or kernel cmdline → identity.env), boot-record (oneshot + ExecStop clean marker, boot_id per start), restarts-metric timer (NRestarts) | implementer | T6.2 | todo | — | |
| T6.4 | Fluent Bit config: scrape node_exporter → remote_write; journald units inference-app, ota-agent, systemd; kmsg only when RUNTIME=firecracker; tail boot.log → Loki; labels device_id, hw_rev, region, fw_version from labels.env | implementer | T6.1, T6.3 | todo | — | Dry-run in the fluent-bit container |
| T6.5 | Build image; static checks (hadolint if cheap, fluent-bit dry-run, one container boots to `running`); tear down | verifier | T6.4 | todo | — | Flags: `--cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock` |
| T6.6 | Smoke S2: Prometheus + Loki + Grafana (capped) + one device container: labeled metrics and logs arrive; fw_version label changes after local install; tear down | verifier | T6.5, T2.4, T2.5, T10.1 | todo | — | |
| T6.7 | Smoke S3: v1.2 on one rev B container with short leak timer; OOM kills + restarts visible in metrics and logs; rev A container stays flat; tear down | verifier | T6.6 | todo | — | |

### M7 — Container runtime + fleet launcher + seed

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T7.1 | `sim/fleet/fleet.yaml`: profiles lite (4) and full (20), hw_rev mix 60/40, regions us-east, eu-west, ap-south, initial_version v1.0, MemoryMax per profile, VM memory per profile | scaffolder | — | done | static | yaml lint. rev B rule: device i is B iff ceil(i*0.4) > ceil((i-1)*0.4) |
| T7.2 | Fleet launcher `sim/fleet/fleetctl.py`: up/down/status/logs; runtime container (docker CLI, field network, systemd flags); deterministic IDs edge-001..; deterministic hw_rev/region assignment; pytest for the assignment and command generation | implementer | T7.1, T6.2 | todo | — | Firecracker backend added in T8.4 |
| T7.3 | `scripts/seed.sh`: wait until all fleet devices are registered in hawkBit, assign DS v1.0 to all, wait for actions to close | orch | T3.2, T7.2 | todo | — | |
| T7.4 | Makefile `fleet`, `fleet-down`, `seed` with PROFILE and RUNTIME | scaffolder | T7.2, T7.3 | todo | — | |

### M8 — Firecracker runtime

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T8.1 | `sim/runtime/firecracker/fetch.sh`: latest release binary to ~/.local/bin; guest kernel from newest `firecracker-ci/vX.Y/<arch>/vmlinux-6.1.*` listing (fall back to older CI dirs) to ~/.local/share/firecracker | scaffolder | — | done | static | shellcheck; idempotent no-op run; `--dry-run` |
| T8.2 | `sim/runtime/firecracker/build-rootfs.sh`: docker build → docker export → `fakeroot mkfs.ext4 -d` (rootless) → base.ext4; per-VM copy with `cp --reflink=auto --sparse=always` | orch | T6.3 | todo | — | fakeroot and mkfs.ext4 -d present locally |
| T8.3 | Guest networking and identity: kernel `ip=` arg for static IP on field or lab bridge, `wavebreak.*` cmdline args → identity unit; RUNTIME=firecracker enables kmsg input | orch | T8.2 | todo | — | |
| T8.4 | Firecracker backend in fleetctl: per-VM config JSON, API socket, serial log, pidfile under `run/fc/<id>/`; start/stop/status N VMs; tap assignment fc-field-N / fc-lab-N; memory 128 MB lite, 256 MB full | orch | T8.3, T7.2 | todo | — | |
| T8.5 | Smoke S4: boot one VM (128 MB) to multi-user; ota-agent /status reachable; telemetry reaches backend if S2 stack is up; tear down | verifier | T8.4, N1 | todo | — | Needs host net (Needs human N1) |

### M9 — Lab controller

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T9.1 | `lab/controller` (FastAPI): POST /lab/devices, POST /lab/devices/{id}/install, GET /lab/devices/{id}/summary, DELETE /lab/devices/{id}, GET /lab/devices; token auth; backend abstraction container or firecracker; read-only hawkBit artifact download; memory trend sampler; pytest with fake device and fake hawkBit | implementer | T5.3, T7.2 | todo | — | Agent gets only this API |
| T9.2 | Lab wiring: controller service in compose (attached to backend + lab networks), lab devices on internal lab network, Makefile `lab` | scaffolder | T9.1, T2.3 | todo | — | Controller needs docker socket; the agent never does |
| T9.3 | Smoke S5: create one lab device, install a bundle fetched from hawkBit, read summary; tear down | verifier | T9.2, T3.3 | todo | — | |

### M10 — Grafana + MCP

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T10.1 | Grafana provisioning: Prometheus + Loki datasources (fixed UIDs), dashboard provider | scaffolder | T2.3 | todo | — | |
| T10.2 | Dashboard "Wavebreak Fleet": devices by fw_version and hw_rev, app memory per device, restarts, OOM kills (Loki), boot events, rollout annotations | implementer | T10.1 | todo | — | JSON lint |
| T10.3 | Research + add mcp-grafana service: official image, network transport flag (SSE or streamable HTTP), port, env for URL and service-account token | researcher | T2.3 | done | static | grafana/mcp-grafana: `-t streamable-http -address 0.0.0.0:8000`, `--disable-write`, env GRAFANA_URL + GRAFANA_SERVICE_ACCOUNT_TOKEN. Compose service added in T2.3 |
| T10.4 | `scripts/grafana-sa.sh`: create Viewer service account + token via Grafana API, write GRAFANA_SA_TOKEN to .env, idempotent | implementer | T10.1 | todo | — | |

### M11 — wavebreak_clients

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T11.1 | `wavebreak_clients/hawkbit.py`: list targets (attributes, installed DS), list DS, create rollout (filter query, groups, success/error conditions), start/pause/resume/stop, rollout + group status, assign DS, action status, download artifact; fixture tests | implementer | T2.1 | todo | — | Endpoints verified in T2.1 or S1 |
| T11.2 | `wavebreak_clients/observability.py`: PromQL instant/range, LogQL range (direct; Grafana proxy optional); fixture tests | implementer | — | done | unit | stdlib urllib; shared `_http.py`; 14 tests vs local http.server fixtures; Grafana proxy constructor |
| T11.3 | `wavebreak_clients/lab.py`: all lab controller endpoints; tests against FastAPI TestClient | implementer | T9.1 | todo | — | |

### M12 — AWS + runbook + final docs

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T12.1 | Research: AWS CLI syntax for nested virtualization on M8i (cpu-options), minimum CLI version, Ubuntu 24.04 SSM parameter path | researcher | — | done | static | `--cpu-options NestedVirtualization=enabled`, AWS CLI >= 2.36, SSM Ubuntu 24.04 path; docs read only |
| T12.2 | `infra/aws/launch.sh`: variables at top, SG restricted to caller IP (22, 3000, 8080, MCP, lab API), key pair, 60 GB gp3, user-data → install.sh | implementer | T12.1 | todo | — | untested, mark clearly |
| T12.3 | `infra/aws/install.sh` (idempotent): Docker, firecracker + kernel, setup-fc-net.sh, repo clone or rsync fallback, make up PROFILE=full → bundles → publish → fleet (firecracker, fallback container if no /dev/kvm) → seed → grafana-sa → print URLs and tokens | implementer | T12.2, T7.4, T8.1 | todo | — | |
| T12.4 | Makefile complete: up, down, bundles, publish, fleet, fleet-down, seed, lab, view, test, lint, smoke-*, all; `.env.example` complete (Grafana + MCP, hawkBit Mgmt + MCP, lab controller, DDI token) | scaffolder | T12.3 | todo | — | |
| T12.5 | Final pass: docs match code, runbook section in architecture.md (AWS + local fallback), STATUS: COMPLETE | orch | all | todo | — | |

## Needs human

| ID | Item | Command | Status |
|----|------|---------|--------|
| N1 | Firecracker host networking (bridges, NAT, user-owned taps). Not persistent: rerun after `wsl --shutdown` | `sudo /home/dev/wavebreak/scripts/host/setup-fc-net.sh` | done 2026-09-25: wbfield0, wblab0, fc-field-1..2, fc-lab-1..2 owned by uid 1001, NAT rule present |
| N2 | Push commits so EC2 can clone | `git push -u origin master` | done 2026-09-25 |

## Parked

| ID | Reason | What the build day needs to do |
|----|--------|-------------------------------|
| — | — | — |

## Run log

| When (local) | Run | Tasks completed |
|--------------|-----|-----------------|
| 2026-09-25 22:15 | 1 (interactive) | Part A: prompt saved, pre-flight, T1.1–T1.3, tracker, subagents, overnight loop |
| 2026-09-25 22:30 | 1 (interactive, after go) | Re-checked N1 (host net up) and N2 (pushed); loop ready |
| 2026-09-26 | MVP run | T2.3–T2.7 backend compose, configs, hawkbit-config.sh, fetch-hawkbit-mcp.sh |
