# Wavebreak — Implementation Tracker

**Objective**: finish the container-runtime MVP critical path from `docs/prompts/overnight.md`. Part A is complete; Firecracker and AWS launch remain parked. Do not edit `agent/`.

- Instructions: [docs/prompts/overnight.md](prompts/overnight.md)
- Design source of truth: [docs/architecture.md](architecture.md)

STATUS: IN PROGRESS

## How to use this file (loop runs)

1. Pick the first task with status `todo` or `in-progress` whose dependencies are all `done` (or `parked` when the task can proceed without them).
2. Mark it `in-progress`, commit. Do it. Verify at the cheapest meaningful level; respect the ~2.5 GB Docker memory limit and run one smoke test at a time.
3. Mark it `done` / `parked` / `blocked`, fill in **Verif** and **Notes**, add a Run log line, commit.

**Status values**: todo, in-progress, done, parked, blocked.
**Verification levels**: static (lint, config check, dry-run) → unit (pytest) → smoke (ran against real components) → unverified (code only). Add `unverified-local` when the code targets EC2 and cannot run in WSL.
**Owners**: `orch` = current session.
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
| T2.3 | `platform/docker-compose.yml`: networks backend, field, lab (lab `internal: true`); services hawkbit (lite H2, heap cap), prometheus (`--web.enable-remote-write-receiver`), loki, grafana; memory limits; `.env` driven; profile `full` adds hawkBit DB | scaffolder | T2.1 | done | smoke | Networks backend, wavebreak_field, wavebreak_lab (internal). Profiles full (MySQL), mcp (jar). hawkBit booted lite; lab user can read software modules and download artifact, target creation returns 403. Image has no curl, no actuator: wait on /v3/api-docs |
| T2.4 | `platform/prometheus/prometheus.yml` (self-scrape only; devices push via remote_write) + `promtool check config` | scaffolder | T2.3 | done | static | promtool check config SUCCESS |
| T2.5 | `platform/loki/config.yaml` (single binary, filesystem, retention small, low memory) | scaffolder | T2.3 | done | static | loki -verify-config rc 0 (3.5.0) |
| T2.6 | Makefile `up` / `down` with PROFILE=lite or full | scaffolder | T2.3 | done | static | Makefile uses --env-file .env; adds mcp profile when jar present |
| T2.7 | hawkBit tenant bootstrap script `scripts/hawkbit-config.sh`: enable gateway-token auth, set token from `.env`, set polling interval | orch | T2.1 | done | smoke | Verified against running server. Min polling 30s by default; lowered with -Dhawkbit.controller.minPollingTime=00:00:05, 10s accepted |
| T2.8 | Persist lite-profile H2 state across hawkBit container recreation | orch | T2.3 | done | smoke | Configured file-backed H2 in the writable artifact volume with `MODE=LEGACY`; republished v1.0–v1.4, restarted hawkBit, and verified all five distribution sets remained. |

### M3 — Bundles + publish

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T3.1 | `scripts/build-bundles.sh`: tar each `sim/bundles/vX.Y` into `build/bundles/wavebreak-app-vX.Y.tar` + `.sha256`; deterministic tar (sorted, fixed mtime/owner) | scaffolder | T4.2 | done | static | Deterministic (sorted, fixed mtime/owner; rebuild gives same sha). Tar root: manifest.json, config.yaml, app/ |
| T3.2 | `scripts/publish-bundles.sh`: discover SM and DS types via Management API; create SM per version, upload artifact, create DS; idempotent (lookup by name+version) | orch | T2.1, T3.1 | done | smoke | Ran twice against hawkBit 1.1.0: 5 SM + 5 DS created, second run no-op. POST bodies take type KEYS (application, app), not ids |
| T3.3 | Smoke S1: hawkBit publish and DDI ota-agent install | orch | T2.7, T3.2, T5.2 | done | smoke | User confirms bundles/publish and DDI verified live against hawkBit; commits a167cdb and eece8f5 |

### M4 — inference-app

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T4.1 | inference-app v1.0 (Python stdlib): camera loop, frame size by HW_REV (A 1080p, B 4K, scaled bytes, configurable), JSON log lines with fw_version and labels, textfile metrics every 5s (atomic write) incl. cgroup memory.current, config reload every ~45s, heartbeat metric; pytest | implementer | — | done | unit | inference_app.py stdlib; tests identity, metrics, parser |
| T4.2 | Releases `sim/bundles/v1.0` … `v1.4` (manifest.json, app/, config.yaml) as real code diffs: v1.1 latency metric; v1.2 unbounded 4K enhancement buffer (rev B only, leak pace configurable, OOM ~3 min); v1.3 renamed config key read on first reload → crash; v1.4 bounded buffer. Tests for leak and crash logic | implementer | T4.1 | done | unit | 41 tests incl. test_faults.py: v1.2 unbounded buffer rev B only, v1.3 KeyError on first reload (uncaught, exit 1), v1.4 bounded. Leak ~12 MB/min at defaults (40 KB 4K frames, 5 fps) |

### M5 — ota-agent

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T5.1 | Install core (shared): extract to inactive slot, flip `/opt/app/current`, restart unit, health window (default 15s: unit active + heartbeat advancing), rollback on failure, rewrite labels.env, restart fluent-bit; systemctl/paths injectable for tests; pytest | orch | T4.1 | done | unit | `sim/ota-agent/ota_agent/install.py`; 12 tests (fake systemd + clock): healthy, rollback on crash, no heartbeat + grace, sha mismatch, traversal, labels.env |
| T5.2 | DDI mode: poll with backoff, configData on registration, deploymentBase handling, download + sha256 check against hawkBit hash, feedback proceeding/success/failure, same-version short-circuit, auth flag gateway-token or target-token; pytest against a mock DDI server | orch | T5.1, T2.1 | done | unit + smoke | Live vs hawkBit 1.1.0 (real DdiAgent, fake systemd): register + configData, install finished, failing install -> rollback + error feedback, same version -> finished. tests/test_ddi.py 9 tests vs mock DDI with captured response shapes |
| T5.3 | Local mode: `ota-agent install <bundle.tar>` CLI + HTTP endpoint (token) POST bundle upload, GET /status; pytest | implementer | T5.1 | done | unit | local.py (POST /install raw tar + X-Sha256, GET /status, Bearer) + __main__.py CLI (ddi, serve, install, status). tests/test_local.py 8 tests. 29 ota-agent tests total |

### M6 — Device image

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T6.1 | Research Fluent Bit: exact plugin names and options for prometheus_scrape, prometheus_remote_write, systemd (journald), kmsg, tail, loki (labels from env, record accessor), and dry-run flag; pinned version and Debian install method | researcher | — | done | static | architecture.md §5 Fluent Bit table; v5.1.2 apt repo |
| T6.2 | `sim/device/Dockerfile`: debian bookworm-slim, systemd PID 1 (mask udev/getty), node_exporter (pinned, textfile collector dir), fluent-bit, python3, ota-agent, inference-app v1.0 preinstalled in slot_a, STOPSIGNAL SIGRTMIN+3 | orch | T4.2, T5.3 | done | smoke | Rebuilt `wavebreak-device:local` from current source; systemd container boot verified in T6.5 |
| T6.3 | systemd units: inference-app (MemoryMax from profile env, Restart=always), ota-agent (mode from env), node_exporter, fluent-bit, wavebreak-identity, boot-record and restarts-metric timer | orch | T6.2 | done | smoke | Latest image boot: identity, inference-app, node_exporter, ota-agent active; e2e exercised OOM restart and restart metric. |
| T6.4 | Fluent Bit config for metrics and logs; kmsg only on Firecracker; labels from labels.env | orch | T6.1, T6.3 | done | smoke | Lite fleet metrics reached Prometheus with device/fw labels and device logs reached Loki; Fluent Bit restarted on OTA label changes. |
| T6.5 | Build image; static checks and one container boots to `running`; tear down | orch | T6.4 | done | smoke | Rebuilt `wavebreak-device:local` from current source. Fresh systemd container booted with identity, app, node_exporter, and local ota-agent active; labels correct and `WAVEBREAK_FRAME_SCALE=0.7` persisted. Removed smoke container. |
| T6.6 | Smoke: backend plus one device container sends labeled telemetry; install updates fw_version labels; tear down | orch | T6.5, T2.4, T2.5 | done | smoke | Lite fleet sent device/fw_version-labeled app metrics to Prometheus and logs to Loki. During e2e, edge-001 metrics changed through v1.2 and v1.1 after installs; fleet was removed afterward. |
| T6.7 | E2E: v1.2 on rev B shows OOM restarts; v1.1 recovery; tear down | orch | T6.6 | done | smoke | `scripts/e2e.sh` assigned v1.2 to rev-B edge-001, observed `wavebreak_app_restarts_total` reach 1, reinstalled v1.1, confirmed active app + labeled frames metric, and removed the lite fleet. |

### M7 — Container runtime + fleet launcher + seed

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T7.1 | `sim/fleet/fleet.yaml`: profiles lite (4) and full (20), hw_rev mix 60/40, regions us-east, eu-west, ap-south, initial_version v1.0, MemoryMax per profile, VM memory per profile | scaffolder | — | done | static | yaml lint. rev B rule: device i is B iff ceil(i*0.4) > ceil((i-1)*0.4) |
| T7.2 | Fleet launcher `sim/fleet/fleetctl.py`: up/down/status/logs; runtime container; deterministic IDs and assignments | orch | T7.1, T6.5 | done | smoke | Implemented Docker field-network lifecycle and env wiring; `make fleet PROFILE=lite RUNTIME=container` launched 4 devices, all expected services active and all four registered in hawkBit. |
| T7.3 | `scripts/seed.sh`: wait until all fleet devices are registered in hawkBit, assign DS v1.0 to all, wait for actions to close | orch | T3.2, T7.2 | done | smoke | `make publish` populated current H2 server; `PROFILE=lite make seed` assigned v1.0 and all 4 agents reported installedDS v1.0. |
| T7.4 | Makefile `fleet`, `fleet-down`, `seed` with PROFILE and RUNTIME | orch | T7.2, T7.3 | done | smoke | Existing targets verified: fleet launch/status and `PROFILE=lite make seed` all succeeded. |

### M8 — Firecracker runtime

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T8.1 | Firecracker fetch script | orch | — | parked | — | Explicitly excluded from MVP critical path |
| T8.2 | Firecracker rootfs build | orch | T6.3 | parked | — | Explicitly excluded from MVP critical path |
| T8.3 | Firecracker guest networking and identity | orch | T8.2 | parked | — | Explicitly excluded from MVP critical path |
| T8.4 | Firecracker backend in fleetctl | orch | T8.3, T7.2 | parked | — | Explicitly excluded from MVP critical path |
| T8.5 | Firecracker VM smoke | orch | T8.4, N1 | parked | — | Explicitly excluded from MVP critical path |

### M9 — Lab controller

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T9.1 | Minimal container lab API: create devices at requested version/revision, install read-only hawkBit artifacts, summary, list, delete; token auth and memory/OOM reporting | orch | T5.3, T7.2 | done | smoke | Created rev-B lab-001 at v1.1 from a verified hawkBit download; summary showed active unit, 11.9 MB app cgroup memory, 0 restarts/OOMs; list and delete worked. |
| T9.2 | Lab wiring: controller service in compose (attached to backend + lab networks), lab devices on internal lab network, Makefile `lab` | orch | T9.1, T2.3 | done | smoke | `make lab PROFILE=lite` built and started controller; `/healthz` returned ready. Controller owns Docker socket; device isolated on `wavebreak_lab`. |
| T9.3 | Smoke S5: create one lab device, install a bundle fetched from hawkBit, read summary; tear down | verifier | T9.2, T3.3 | done | smoke | Live hawkBit lab user reads module/artifact (200) but cannot create target (403); one rev-B device installed v1.1 and was removed after summary. |

### M10 — Grafana + MCP

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T10.1 | Grafana provisioning: Prometheus + Loki datasources (fixed UIDs), dashboard provider | orch | T2.3 | done | static | Provisioning files are present; verify in backend smoke |
| T10.2 | Minimal fleet dashboard for versions, memory, restarts and OOM evidence | orch | T10.1 | todo | — | Critical path |
| T10.3 | Research + add mcp-grafana service: official image, network transport flag (SSE or streamable HTTP), port, env for URL and service-account token | researcher | T2.3 | done | static | grafana/mcp-grafana: `-t streamable-http -address 0.0.0.0:8000`, `--disable-write`, env GRAFANA_URL + GRAFANA_SERVICE_ACCOUNT_TOKEN. Compose service added in T2.3 |
| T10.4 | `scripts/grafana-sa.sh`: create Viewer service account + token via Grafana API, write GRAFANA_SA_TOKEN to .env, idempotent | implementer | T10.1 | todo | — | |

### M11 — wavebreak_clients

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T11.1 | `wavebreak_clients/hawkbit.py`: read inventory and create/control one explicitly-started rollout per wave; no automatic next-group start; assignment and artifact download | orch | T2.1 | done | smoke | Live hawkBit OpenAPI verified. Created an unstarted rollout for edge-001; it settled at ready with one group while installedDS remained v1.0. start is a separate method. Ruff passes. |
| T11.2 | `wavebreak_clients/observability.py`: PromQL instant/range, LogQL range (direct; Grafana proxy optional); fixture tests | implementer | — | done | unit | stdlib urllib; shared `_http.py`; 14 tests vs local http.server fixtures; Grafana proxy constructor |
| T11.3 | `wavebreak_clients/lab.py`: all lab controller endpoints; tests against FastAPI TestClient | implementer | T9.1 | todo | — | |

### M12 — AWS + runbook + final docs

| ID | Task | Owner | Depends | Status | Verif | Notes |
|----|------|-------|---------|--------|-------|-------|
| T12.1 | Research: AWS CLI syntax for nested virtualization on M8i (cpu-options), minimum CLI version, Ubuntu 24.04 SSM parameter path | researcher | — | done | static | `--cpu-options NestedVirtualization=enabled`, AWS CLI >= 2.36, SSM Ubuntu 24.04 path; docs read only |
| T12.2 | AWS `launch.sh` | orch | T12.1 | parked | — | Explicitly excluded from MVP critical path |
| T12.3 | AWS `install.sh` | orch | T7.4, T8.1 | parked | — | AWS and Firecracker deployment excluded from MVP critical path |
| T12.4 | Verify Makefile and `.env.example` cover the container MVP | orch | T7.4 | todo | — | Do not add unrelated full/AWS targets |
| T12.5 | Final pass: exact local container runbook; docs match code; STATUS: COMPLETE | orch | all critical path tasks | todo | — | |

## Needs human

| ID | Item | Command | Status |
|----|------|---------|--------|
| N1 | Firecracker host networking (bridges, NAT, user-owned taps). Not persistent: rerun after `wsl --shutdown` | `sudo /home/dev/wavebreak/scripts/host/setup-fc-net.sh` | done 2026-09-25: wbfield0, wblab0, fc-field-1..2, fc-lab-1..2 owned by uid 1001, NAT rule present |
| N2 | Push commits so EC2 can clone | `git push -u origin master` | done 2026-09-25 |

## Parked

| ID | Reason | What the build day needs to do |
|----|--------|-------------------------------|
| Firecracker runtime (T8.1–T8.5) | Outside container-only MVP scope | Resume only if the build requires Firecracker |
| AWS launch (T12.2–T12.3) | Outside MVP scope | Deploy container fallback manually only if needed |
| Extra docs polish, AWS/full profile, optional MCP integration work | Outside MVP scope | Do not resume for this handoff |

## Run log

| When (local) | Run | Tasks completed |
|--------------|-----|-----------------|
| 2026-09-25 22:15 | 1 (interactive) | Part A: prompt saved, pre-flight, T1.1–T1.3, tracker, subagents, overnight loop |
| 2026-09-25 22:30 | 1 (interactive, after go) | Re-checked N1 (host net up) and N2 (pushed); loop ready |
| 2026-09-26 | MVP run | T2.3–T2.7 backend compose, configs, hawkbit-config.sh, fetch-hawkbit-mcp.sh |
| 2026-09-26 | Codex handoff audit | Reconciled tracker with repository; Part A skipped; container MVP scope recorded; agent-design pointer added |
| 2026-09-26 | Codex MVP | Built device image and booted one container; fixed identity startup temp-file bug (T6.5) |
| 2026-09-26 | Codex MVP | Implemented fleetctl container up/down/status/logs; lite fleet booted and registered (T7.2) |
| 2026-09-26 | Codex MVP | Published bundles to live H2 hawkBit and seeded all four devices (T7.3) |
| 2026-09-26 | Codex MVP | Verified Makefile fleet and seed integration (T7.4) |
| 2026-09-26 | Codex MVP | Added hawkBit Management API client; verified a ready one-group rollout stays unstarted (T11.1) |
| 2026-09-26 | Codex MVP | Ran v1.2 OOM → v1.1 recovery e2e on edge-001; fleet cleaned up (T6.7) |
| 2026-09-26 | Codex MVP | Persisted lite H2, republished releases, restarted hawkBit, and verified v1.0–v1.4 survived (T2.8) |
| 2026-09-26 | Codex MVP | Rebuilt latest device image, verified frame-scale env and quick boot, removed smoke container (T6.5) |
| 2026-09-26 | Codex build | Completed and smoke-tested isolated lab controller with read-only hawkBit artifact access; deleted lab device (T9.1–T9.3) |
