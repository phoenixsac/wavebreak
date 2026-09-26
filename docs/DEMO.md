# Wavebreak: 5-minute live demo

## 60-second pitch (say this first)

"Over-the-air updates to camera fleets have a blind spot. The update server says *success* the moment a firmware installs. But the bad releases fail *after* install: a memory leak that only hits one hardware revision, a config change that crashes every device 45 seconds in, after the health check. The server never sees it, and the release keeps spreading.

Wavebreak is an agent that owns the rollout. It rehearses the release on throwaway lab devices, one per hardware revision. It ships in stratified waves, one hawkBit rollout per wave. Every action that changes the field needs fresh evidence from real Prometheus and Loki data, checked by the server, and a human clicking Allow. If only one hardware revision is bad, it ships the healthy one and holds the other, with the evidence in an audit ledger. If data is missing, it says *inconclusive*, never *healthy*. Everything you will see runs against a real hawkBit, Prometheus, Loki and a real rehearsal lab; only the hardware is simulated."

## Before you start (2 minutes, off stage)

- Fleet on v1.1 and healthy: `make demo-reset` then `make demo-status` (4 devices, 2 rev A, 2 rev B, lab empty). On AWS use the same commands on the instance.
- TrueForge chat open with agent `wavebreak`: local `http://localhost:8790`, AWS via the SSH tunnel `http://localhost:18790` (see `docs/aws-deploy.md`). Start a **new session**.
- Grafana dashboard **Wavebreak Fleet** open in a second tab: local `http://localhost:3000`, AWS `http://localhost:13000`. Panels: devices by firmware, app memory, restarts, fps, OOM logs.
- Architecture slides ready: `make view`, `docs/view/index.html`, press `P`.
- Backup terminal for the audit trail (see Scene 2 fallback).
- Keep `wavebreak-hawkbit-ui-1` stopped on the 3.5 GB local host.

## Timeline

| Time | What |
|---|---|
| 0:00 | Pitch (above) |
| 1:00 | Scene 1: type the prompt, narrate while the rehearsal runs (about 3 to 4 minutes on AWS with lab parallel 2, up to 6 locally) |
| 4:00 | Scene 1 report, then Scene 2 as an audit trail from the last run (below) |
| 5:00 | Judge questions |

Running Scene 2 live takes about 10 minutes (two rehearsals plus a 2-minute soak). Only do it if you have the time; otherwise show its audit trail.

## Scene 1: v1.3 is blocked (live)

**Type:** `v1.3 is published. Take care of the rollout.`

Point at, in order:
1. The agent reads the inventory: 4 devices on v1.1, 2 rev A and 2 rev B. It plans wave 1 with one device of each revision.
2. **Rehearse** starts. Say: "It boots a throwaway lab device per hardware revision and installs v1.3. hawkBit would already call this a success." Switch to Grafana: the fleet is flat, because nothing has touched the field.
3. Result: rehearsal **fail on both revisions** (restarts), plan **BLOCKED**. The agent reads the bundle diff and names the cause: a config key renamed without migration, so the app crashes on its first config reload 45 seconds after start, after the install health window.
4. Final report: no field device touched, **no approval was asked for**, fleet still 4 x v1.1.

If the agent stops early and asks to proceed, type `proceed`.

## Scene 2: v1.2 partial rollout

**Live version.** Reset first (`make demo-reset`), new session, **type:** `v1.2 is published. Take care of the rollout.`

1. Rehearsal of all revisions: **rev A passes, rev B fails**. Point at the memory slope (about 12 MB/min against a 1.0 threshold): a 4K buffer that is never evicted, only on the 4K sensor revision. Plan BLOCKED.
2. The agent proposes a **partial rollout**: it plans again with rev B excluded, citing the failed rehearsal's evidence id, and re-rehearses rev A. Say: "The hold is only possible with that evidence; the server refuses it otherwise."
3. **The Allow prompt appears** for `start_wave`. Read the `evidence_summary` in the prompt aloud (devices, verdict per revision, numbers, held cohort, cause). **Click Allow** here, and at any second wave prompt.
4. Grafana: the rev A device moves to v1.2 and stays flat while rev B stays on v1.1.
5. Final report: rev A on v1.2 and healthy; **rev B (edge-001, edge-003) held on v1.1 pending a fix**, with the evidence id.

**If time is short: show the audit trail from the last run.** In the chat, type:
`Show the audit trail of the last v1.2 rollout: plan id, the held cohort and its evidence id, the approvals with their evidence summary, and the decisions.`
(The agent calls `get_rollout_state` with the last plan's id.) Backup without the model, from the repo root:

```bash
.venv/bin/python - <<'EOF'
import sqlite3, json
c = sqlite3.connect("run/fleet-ledger.sqlite")
pid = c.execute("select plan_id from rollouts where version='v1.2' order by created_at desc limit 1").fetchone()[0]
for ts, typ, ev, p in c.execute("select substr(ts,12,8), type, evidence_id, payload_json from events where plan_id=? and type in ('rehearsal','hold','approval','action','observation','decision') order by id", (pid,)):
    d = json.loads(p); print(ts, typ, ev or "", d.get("verdict") or d.get("kind") or d.get("decision") or d.get("evidence_summary","")[:90])
EOF
```

Point at: the `hold` event (rev B, evidence id, source plan), the `approval` event (the evidence summary the human saw), the healthy observation. Say: "Every action is in the ledger with why."

## If the agent stalls

Some models end their turn after announcing an action ("Proceeding to start wave 1"). Nothing has been started. Type **`proceed`**; the agent must then call the tool and the normal Allow prompt follows. Say: "That is the model, not the gate; the server would refuse anyway without evidence." If the evidence is older than 15 minutes the server answers `EVIDENCE_STALE` and the agent re-observes first.

## Likely judge questions

**Why not just hawkBit rollout thresholds?** They only count install failures and success rates. Both of our faults install fine. The leak appears minutes later and only on one revision; the config crash starts 45 seconds after the install health window closes. You need runtime telemetry compared with same-revision controls.

**What if the model is wrong?** It cannot act on its own say-so. Every action tool is checked server-side: right evidence kind, latest of its kind, fresh, right plan and wave, and rehearsal coverage of every hardware revision. Missing or unreachable data becomes `inconclusive`, never `healthy`. And a human approves every wave start, halt and rollback. Worst case is a needless hold or a denied approval, not a bad rollout. Honest caveat: models sometimes stall; we document a `proceed` fallback.

**How is approval enforced?** By the TrueForge harness: `start_wave`, `halt_rollout` and `rollback` are named in `require_approval_for_tools` and annotated destructive, so the call pauses until Allow or Deny, and they cannot be called from the sandbox's Code Mode. The prompt carries a required evidence summary, which is stored in the ledger. Honest caveat: the fleet MCP server trusts the harness (localhost, no auth), so this is a demo-grade trust boundary.

**Why a sandbox?** Model-generated analysis code (statistics over telemetry) should never touch infrastructure. TrueForge's local bubblewrap sandbox has no route to our devices and cannot call the approval-gated tools. It is weaker isolation than a hosted sandbox (shared kernel), and our scripted scenes are driven by MCP tools, so we show the boundary rather than lean on it.

**How does it scale?** The full profile runs 20 devices on one `m8i.2xlarge`. Waves are stratified by hardware revision and spread across regions; tools return summaries, not raw series, so context stays small; state lives in a SQLite ledger plus hawkBit and survives compaction. To grow: page hawkBit target listing (it makes about N+2 calls per device today), run more lab devices in parallel, and add per-region waves. Rehearsal windows are short by design, so later-onset faults are caught by the soak in the next wave.
