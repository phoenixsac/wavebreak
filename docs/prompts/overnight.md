You are the lead engineer and orchestrator for the Wavebreak repo. Read AGENTS.md and docs/architecture.md before anything else.

HOW THIS SESSION WORKS
- Run 1 (now, interactive, the user is present): do PART A (pre-flight + planning files + loop setup). Stop at the checkpoint and wait for the user to reply "go".
- Every later run is started by scripts/overnight.sh with the instruction "Continue per docs/prompts/overnight.md". On those runs: read docs/IMPLEMENTATION.md, skip PART A, and continue PART B from the first task that is not done/parked. Never redo finished work.
- First action in run 1: save this entire prompt verbatim to docs/prompts/overnight.md and commit it, so every later run reads the same instructions.

======================================================================
0. CONTEXT (condensed; AGENTS.md has the rest)
- Hackathon "Agents That Act" (HackCulture; TrueFoundry + Polaris). Build window tomorrow: 6–7 hours. Mandatory stack: TrueFoundry, TrueForge (MIT open-source agent harness from the organizers), AWS. Agents must reach a real system, run generated code in a sandbox, and stop for human approval before irreversible actions.
- Product: Wavebreak, "catches bad OTA rollouts at runtime and rolls them back, with your approval." Tomorrow's agent acts as the rollout manager: inventory device versions → rehearse the update on throwaway devices → canary waves 2 → 5 → all → verify with metrics/logs → promote, halt, or roll back via hawkBit with human approval.
- OBJECTIVE OF THIS RUN: complete ALL code and config for the production-like environment (Part A) so tomorrow's tokens go only into the agent. Do not build the agent. Verification is secondary: this machine has 3.5 GB RAM for WSL (cannot be increased) and the full system runs on AWS tomorrow. Code-complete everywhere; verified where cheap.

======================================================================
1. FROZEN DESIGN (implement exactly; record any deviation in architecture.md §9 decisions log)

1.1 Topology (three zones)
- backend: hawkBit (update server + UI; full profile uses the database from hawkBit's official compose, lite profile uses embedded H2), Prometheus (remote-write receiver enabled), Loki, Grafana (provisioned datasources + fleet dashboard), mcp-grafana (official Grafana MCP server, read-only service account, network transport so a remote agent can reach it), hawkBit MCP server (verify it exists in the release; see edge cases).
- field: the device fleet. Devices only initiate outbound connections to backend endpoints (DDI polling, Prometheus remote_write, Loki push), like real devices behind NAT.
- lab (rehearsal lab): isolated network for throwaway test devices, managed by the lab controller API. Lab devices never talk to production hawkBit, Prometheus or Loki.

1.2 Device (one image, two runtimes)
- Built from sim/device/Dockerfile: Debian slim + systemd as PID 1 + node_exporter + Fluent Bit + ota-agent + inference-app + boot-record service.
- RUNTIME=firecracker (preferred) or RUNTIME=container (systemd container; the guaranteed path and the source of the Firecracker rootfs).
- Identity: DEVICE_ID, HW_REV (A|B), REGION. Container runtime passes env; Firecracker passes kernel cmdline args (e.g. wavebreak.device_id=...) read by a first-boot unit that writes /etc/wavebreak/identity.env.
- A/B app slots: /opt/app/slot_a, /opt/app/slot_b, symlink /opt/app/current.
- inference-app: systemd unit with MemoryMax (lite ~48M, full ~96M, configurable), Restart=always. Simulated camera-AI loop: generates frames (sized by HW_REV sensor: rev A "1080p", rev B "4K"; byte sizes scaled down and configurable), "processes" them, logs structured JSON lines (include fw_version), writes node_exporter textfile metrics every 5s:
  wavebreak_app_info{fw_version}, wavebreak_app_rss_bytes, wavebreak_app_cgroup_memory_bytes (read the unit's cgroup memory.current), wavebreak_app_frames_total, wavebreak_app_fps, wavebreak_app_cache_items, wavebreak_app_restarts_total (systemctl NRestarts, written by a small timer/unit), wavebreak_device_boot_time_seconds.
  Reason: in containers node_exporter reports HOST memory, so app-level and cgroup metrics are the source of truth in both runtimes.
- boot-record service: oneshot at boot, appends a JSON line to /var/log/wavebreak/boot.log {boot_id, ts, fw_version, previous_shutdown: clean|unclean}. Clean-shutdown marker is written by ExecStop. In containers, generate boot_id per container start (the kernel boot_id is the host's).
- Labels: every metric series and log line carries device_id, hw_rev, region, fw_version. After each install, ota-agent rewrites /etc/wavebreak/labels.env and restarts Fluent Bit so static labels update. Also expose fw_version on wavebreak_app_info for joins.
- Fluent Bit: scrape local node_exporter → prometheus_remote_write to backend; inputs journald (units: inference-app, ota-agent, systemd), kmsg (Firecracker only; in containers kmsg is the host's → disable it there and document), tail boot.log → Loki with labels. Verify exact plugin names/options against official Fluent Bit docs.

1.3 ota-agent (Python stdlib only, packaged in sim/ota-agent with pytest tests)
- Mode `ddi` (field): poll hawkBit DDI with backoff; on registration send config data (device_id, hw_rev, region, fw_version) to the DDI configData endpoint; on deploymentBase: download artifact, verify sha256 against hawkBit's hash, install, send feedback (proceeding/success/failure with messages). If assigned version == installed version, report success without reinstalling (this makes seeding work).
- Mode `local` (lab): `ota-agent install <bundle.tar>` CLI plus a small HTTP endpoint (lab only, token-protected) accepting a bundle upload and returning install result, plus GET /status (fw_version, slot, unit state, restarts, last install result).
- Install procedure (shared by both modes): extract to inactive slot → flip symlink → restart unit → health check window (default 15s: unit active + app heartbeat metric advancing) → on failure flip back, restart, report failure. Update labels.env and restart Fluent Bit on success.
- Auth: DDI gateway token (record decision; production would use per-device tokens/mTLS).

1.4 Releases (sim/bundles/v1.0 … v1.4; each: manifest.json, app/, config.yaml). Faults are real code diffs, never fabricated log lines.
- v1.0 baseline.
- v1.1 good; small feature (e.g. adds per-frame latency metric).
- v1.2 adds a "4K enhancement buffer" used only when the sensor is 4K (HW_REV=B); it is never evicted → memory climbs to MemoryMax → OOM kill → restart loop. Rev A never touches this path. Leak pace configurable (default: OOM ~3 min after install in both profiles).
- v1.3 renames a config key without migration. The app reads that key on its first scheduled config reload (~45s after start, i.e. AFTER the install health-check window) and crashes → crash loop on every device, while hawkBit shows install success.
- v1.4 fixes v1.2 with a bounded buffer.
- scripts/build-bundles.sh builds tarballs + sha256 into build/bundles/. scripts/publish-bundles.sh creates software modules, uploads artifacts and creates distribution sets through the hawkBit Management API, idempotently (discover module/DS types via the API at runtime rather than hardcoding).

1.5 Fleet
- sim/fleet/fleet.yaml: profiles lite (4 devices) and full (20 devices); hw_rev mix default 60% A / 40% B; 3 regions; initial_version v1.0 (whole fleet on one version).
- Fleet launcher (Python CLI): up/down/status/logs per runtime. Deterministic device IDs (e.g. edge-001).
- scripts/seed.sh: wait until all devices are registered in hawkBit, then assign v1.0 so hawkBit state matches reality.
- Canary waves 2 → 5 → all are executed by tomorrow's agent; document only.

1.6 Rehearsal lab (lab/controller, Python, FastAPI acceptable, pytest tests)
- POST /lab/devices {count, hw_rev, version} → boots lab devices at that version (local install from a bundle the controller fetched).
- POST /lab/devices/{id}/install {version} → controller downloads the artifact READ-ONLY from hawkBit's Management API and uploads it to the device's local-install endpoint.
- GET /lab/devices/{id}/summary → fw_version, memory trend (samples over time), restarts, OOM kills, unit state, last install result; read directly from the device (status endpoint + node_exporter scrape).
- DELETE /lab/devices/{id}; GET /lab/devices.
- Token auth. The agent gets only this API: no Docker socket, no root.
- Isolation: container runtime → internal Docker network with no route to the backend (controller attaches to both); Firecracker runtime → separate bridge with no NAT.

1.7 Firecracker runtime (sim/runtime/firecracker)
- Fetch the firecracker binary and an official guest kernel as described in Firecracker's getting-started docs (verify locations). Install to ~/.local (no sudo).
- Rootfs: build the device image → docker export → ext4 image, rootless if possible (e.g. mkfs.ext4 -d; handle file ownership); otherwise document the sudo step. Per-VM copy (sparse/reflink where possible).
- Networking: per-VM tap on a bridge (field bridge with NAT to reach backend ports on the host; lab bridge without NAT). scripts/host/setup-fc-net.sh (run once with sudo) creates the bridges, NAT and N user-owned taps so VMs run without root afterwards.
- Per-VM config file, API socket, serial console log, pidfile. Launcher supports start/stop/status for N VMs. 128 MB per VM in lite, 256 MB in full.

1.8 Grafana
- Provisioned Prometheus + Loki datasources and one dashboard "Wavebreak Fleet": devices by fw_version and hw_rev, app memory per device, restarts, OOM kills, boot events, rollout annotations.
- scripts/grafana-sa.sh creates the read-only service account + token for mcp-grafana and writes it to .env.

1.9 Agent-facing prep (NOT the agent): wavebreak_clients/ Python package with tested thin clients tomorrow's agent will wrap as tools:
- hawkbit: list targets with attributes and installed DS, list DS, create rollout (filter query, groups, success/error conditions), start/pause/resume/stop rollout, get rollout/group status, assign DS to targets, get action status, download artifact.
- observability: PromQL instant/range query, LogQL query (via Grafana datasource proxy or direct).
- lab: all lab controller endpoints.
Keep each function small, documented, and matched to real endpoints you verified.

1.10 AWS (infra/aws; untested tonight, clearly marked)
- launch.sh: AWS CLI; m8i.2xlarge (nested virtualization supported on C8i/M8i/R8i, not Graviton) with nested virtualization enabled via CPU options (verify exact CLI syntax; needs a recent AWS CLI), Ubuntu 24.04 AMI via SSM parameter, 60 GB gp3, security group restricted to the caller's IP (22, Grafana, hawkBit, MCP, lab API), key pair, user-data calling install.sh. Variables at top; no hardcoded account data.
- install.sh (idempotent): install Docker, firecracker + kernel, run setup-fc-net.sh, get the repo (REPO_URL clone, or rsync fallback documented), then: make up PROFILE=full → build + publish bundles → fleet up (RUNTIME=firecracker, fall back to container automatically if /dev/kvm missing) → seed → grafana-sa → print all URLs and tokens.
- Local fallback: identical commands work on any Linux box with Docker.

1.11 Makefile targets: up, down, bundles, publish, fleet, fleet-down, seed, lab, view, test, lint, smoke-<component>, all (PROFILE=lite|full RUNTIME=container|firecracker).

1.12 .env.example with every URL/token tomorrow's agent needs: Grafana + MCP, hawkBit Management API + MCP, lab controller.

======================================================================
2. EDGE CASES AND FALLBACKS (park, document, continue; never stall)
- hawkBit MCP missing, not in the Docker image, or needs a heavy Maven build: document findings in architecture.md; do not build it locally (RAM). Tomorrow the agent can wrap wavebreak_clients.hawkbit. Park.
- hawkBit API differs from docs: read the running server's OpenAPI/Swagger and adapt; record endpoint facts in architecture.md §5.
- Device registration fails (auth mode, tenant, token): try gateway token and target-token modes; log the exact error; implement both behind a config flag if needed.
- systemd container fails under WSL cgroups: try the standard flags (privileged or cgroupns host, cgroup mount rw, tmpfs /run and /run/lock, STOPSIGNAL SIGRTMIN+3). If it still fails locally, keep the code (it targets native Linux on EC2), mark "unverified-local", continue.
- Firecracker fails locally (kvm permissions, nested virtualization, rootless rootfs build): keep complete code, mark "unverified-local", continue; AWS m8i is the real target.
- Fluent Bit plugin mismatch: fix per docs; if remote_write from Fluent Bit is impossible, fall back to Prometheus agent mode on the device and record the decision.
- Loki/Prometheus/hawkBit OOM locally: lower limits; never run more than one heavy component group at a time.
- Image pulls or downloads blocked: record in "Needs human" and continue with other tasks.
- Usage limits: just exit cleanly after committing; the loop restarts you.

======================================================================
3. VERIFICATION POLICY (RAM budget ~2.5 GB; stop containers after every smoke test)
Levels, recorded per task in IMPLEMENTATION.md: static → unit → smoke → unverified.
- Always (cheap): docker compose config, promtool check config, Fluent Bit dry-run in its container, shellcheck, ruff/pyflakes, pytest (ota-agent against a mock DDI server, app leak/crash logic, lab controller with a fake device, clients against recorded fixtures), yaml/json lint.
- Smoke tests, one at a time, then tear down:
  S1 hawkBit alone (H2, JVM heap capped ~512 MB): publish-bundles.sh + one ota-agent process (or one device container) registers, gets v1.1, installs, reports success.
  S2 Prometheus + Loki + Grafana (capped) + ONE device container: labeled metrics and logs arrive; fw_version label changes after a local install.
  S3 v1.2 in one rev B device container with a short leak timer: OOM kills and restarts show up in metrics/logs; rev A device stays flat.
  S4 Firecracker: boot ONE VM (128 MB) to multi-user; ota-agent status endpoint reachable; telemetry reaches S2's backend if running.
  S5 lab controller: create one lab device, install a bundle fetched from hawkBit, read the summary.
- Never run the full fleet locally.

======================================================================
4. MODEL AND TOKEN DISCIPLINE
- You (main session) are the orchestrator: plan, make design calls, review, and handle the hardest pieces (install semantics in ota-agent, Firecracker runtime, hawkBit integration).
- Create project subagents in .claude/agents/ with model frontmatter:
  - scaffolder (model: haiku): boilerplate, configs, Makefile, compose files, docs formatting.
  - implementer (model: sonnet): feature code + tests from a precise spec you write.
  - verifier (model: haiku): runs lint/tests/smoke commands and returns a SHORT pass/fail summary with the key error lines only.
  - researcher (model: haiku): looks up specific facts in official docs (hawkBit API, Fluent Bit plugins, Firecracker setup, AWS CLI flags) and returns only the answer + URL.
- Delegate every easy or mechanical task to the cheapest capable subagent. Give subagents precise, self-contained specs; ask for concise results. Don't paste large files into context unnecessarily; read only what the current task needs.
- One task at a time; small commits.

======================================================================
PART A — RUN 1 ONLY (interactive)
A1. Save this prompt to docs/prompts/overnight.md; commit.
A2. Pre-flight checks. Run each; fix anything fixable without sudo; for anything needing the user, print the exact commands in one block:
  - docker + docker compose v2 work; disk free ≥ 20 GB; RAM figures.
  - systemd container test: start a minimal Debian+systemd container with the flags from §2 and confirm systemctl works; clean up.
  - /dev/kvm usable by this user (if not: `sudo usermod -aG kvm $USER` then restart WSL with `wsl --shutdown` from Windows).
  - Firecracker binary downloadable to ~/.local/bin.
  - Write scripts/host/setup-fc-net.sh now and ask the user to run it once with sudo (bridges, NAT, 2 user-owned taps for local smoke tests).
  - python3 + venv + pip, git, jq, curl, make, tmux, shellcheck (print `sudo apt install ...` for missing ones).
  - claude CLI present and supports subagent model frontmatter and headless mode (`claude -p`).
  - git remote configured? If not, note it (tomorrow's EC2 clone needs it; rsync fallback exists).
  - Remind the user: keep the laptop awake and plugged in overnight.
A3. Write docs/IMPLEMENTATION.md:
  - Header: objective, links to this prompt and architecture.md, a STATUS line (IN PROGRESS | COMPLETE).
  - Task table covering every item in §1 and §3, with columns: ID, task, owner (subagent/model), depends on, status (todo | in-progress | done | parked | blocked), verification level, notes. Group into milestones M1–M12: docs update, backend compose, bundles + publish, inference-app, ota-agent, device image, container runtime + fleet launcher + seed, Firecracker runtime, lab controller, Grafana + MCP, wavebreak_clients, AWS + runbook + final docs.
  - Sections: Needs human; Parked (with reason and what tomorrow needs to do); Run log (one line per run: timestamp, tasks completed).
A4. Create .claude/agents/ subagents from §4.
A5. Create scripts/overnight.sh:
  - Loop: run `claude -p "Continue per docs/prompts/overnight.md" --dangerously-skip-permissions --max-turns <large>` with the strongest available model for the orchestrator; append output to logs/overnight.log.
  - After each run: stop if IMPLEMENTATION.md has "STATUS: COMPLETE"; if the output indicates a usage limit, sleep 30 minutes; otherwise sleep 1 minute; hard cap on iterations; print timestamps.
  - Verify the exact CLI flags against the local `claude --help` before finalizing.
A6. Update AGENTS.md (environment findings, repo map, subagents, loop) and architecture.md with §1 (C4 L1–L3, sequences: healthy update, faulty rollout, rollback, rehearsal; interfaces; data; fault catalogue; deployment; decisions). Run make view. Commit.
A7. CHECKPOINT: print a short summary: pre-flight results, the sudo/human commands block, and "Reply 'go' after running these." Wait.
After "go": re-check anything the user fixed, update Needs human, commit, then tell the user: exit this session, run `tmux new -s night`, then `./scripts/overnight.sh`, and detach. Do not start PART B in this interactive session.

======================================================================
PART B — EVERY LOOP RUN
B1. Read docs/IMPLEMENTATION.md (and only the parts of architecture.md needed for the next task).
B2. Pick the first task that is todo or in-progress whose dependencies are done. Mark it in-progress and commit.
B3. Delegate or implement. Verify at the cheapest meaningful level (§3).
B4. Immediately after finishing: mark it done (or parked/blocked with reason and next step), record the verification level, update architecture.md if the design or interfaces changed, run make view if docs changed, add a run-log line, commit with a clear message.
B5. Repeat until out of turns or budget. Commit before exiting; never leave uncommitted work.
B6. When every task is done or parked: make a final consistency pass (docs match code, .env.example complete, runbook section in architecture.md with tomorrow's exact commands for AWS and for the local fallback), set "STATUS: COMPLETE", commit.

RULES (always)
- Docs and code change together. Never hand-edit docs/view output.
- Never claim something works without running it; label verification honestly.
- Never fabricate telemetry or logs; symptoms must come from real processes misbehaving.
- No secrets in git. No employer code or data.
- Prefer finishing more tasks at "static/unit" verification over sinking hours into one local smoke test that the memory limit won't allow.
