# Wavebreak agent: status (2026-09-26, after the gateway / fail-closed round)

## Result of the final Scene 2 run (openai-polaris/gpt-4o via the TrueFoundry AI Gateway)
- Model: `tfy-gateway/openai-polaris-gpt-4o` (demo tier, `TFY_MODEL`), lab parallel 1, hawkBit UI stopped, `make demo-reset` first, exact prompt `v1.2 is published. Take care of the rollout.`, driver `--auto-approve` (the one approval is logged in the ledger and the driver log).
- **Completed cleanly, no "proceed" nudge.** Rehearsal of all revs: rev A pass, rev B fail (memory slope 12.25 MB/min), plan BLOCKED; partial plan for rev A with a `hold` event citing the fresh failed evidence `ev-75a9eede`; rev A rehearsal pass; `start_wave` (evidence summary shown in the approval); observation healthy; plan COMPLETE.
- End state verified with `make demo-status`: edge-002 and edge-004 (rev A) on v1.2 and healthy, edge-001 and edge-003 (rev B) on v1.1 and healthy.
- **Duration: 644 s (10 min 44 s).** Over the 8-minute target: rehearsal of both revs 5 min 23 s, rev A rehearsal 2 min 52 s, one 2 min soak (lab parallel 1).
- **Tokens and cost** (TrueForge session events via `agent/usage.py`): 8 LLM calls, 64,349 input tokens (58,368 cached), 1,623 output tokens, total 65,972, cost $0.104, 2 turns. For comparison the dev model `gpt-4.1-mini` on the same scene: 7 calls, 59,419 in, 834 out, $0.010, 353 s. The event-based count reflects what TrueForge exposes (one `model.message` per LLM call).
- Deviations from the storyline: gpt-4o used the default waves, so wave 1 held both rev A devices (one approval, not two); the final report quoted an old evidence id (`ev-91764613`, from an earlier run) as the hold evidence, although the ledger's hold event correctly cites `ev-75a9eede`.

## What I changed this round (all committed, tests: 416 pass, ruff clean on `agent/fleet_mcp`, `driver.py`, `register.py`, `usage.py`)
1. **`start_wave` coverage (drift C1):** refuses with `REHEARSAL_COVERAGE` unless the rehearsal evidence covers every hw_rev of the plan (wave 1) or of the wave (later waves, over all rehearsals of the plan); held cohorts need nothing. The message names the uncovered hw_rev and says to run `rehearse`.
2. **Fail closed (drift C3):** Prometheus or Loki errors (`MetricsUnavailable`), missing series, missing control and baseline, an unevaluable rehearsal slope and lab failures give `inconclusive` with reasons for `observe_wave`, `rehearse` and `verify_recovery`; inconclusive evidence cannot start a wave.
3. **Approval prompt is the evidence card:** required `evidence_summary` (at least 20 characters) on `start_wave`, `halt_rollout`, `rollback`; recorded in the approval ledger event; the agent calls the tool directly; the "proceed" fallback is documented in `docs/agent-design.md` §20.
4. **Model via the TrueFoundry AI Gateway:** `agent/register.py --provider gateway|openai|google-gemini --tier dev|demo --model --register-provider` (OpenAI-compatible `custom` provider `tfy-gateway`, base URL `TFY_BASE_URL/v1`, models `TFY_MODEL` = `openai-polaris/gpt-4o` and `TFY_MODEL_DEV` = `openai-polaris/gpt-4.1-mini`); Gemini is the documented fallback; the 429 backoff stays in the driver; `agent/usage.py` reports tokens and cost per session. Decision A28 in §23.
5. **Stale-history fixes found by the test runs:** `get_rollout_state` without a plan id now returns only an ACTIVE plan (finished plans appear as `last_plan`; A29); a hold now needs fresh failed-rehearsal evidence (A30); the instructions say terminal plans are history and the inventory decides.

## Still flaky / not verified
- **Models stop mid-flow.** `gemini-3-5-flash-lite` ended turns after writing a card; `gpt-4o` stopped after the inventory twice when the ledger held a finished plan for the same version (fixed by A29, then it ran clean once). Fallback for the demo: if the agent ends its turn after proposing an action, type `proceed` (`agent/driver.py --session <id> "proceed"`); it must then call the tool (documented in §20). Only one clean gpt-4o run exists; do not treat it as a guarantee.
- gpt-4o did not follow `waves=[1,"rest"]` and misquoted an old evidence id in the report. Consider a stronger tier or a server-side default of `[1, "rest"]` when a hold is present.
- Scenes 1 and 3 were not re-run on gpt-4o after these changes (you asked for Scene 2 only). Scene 3 (Gemini demo model, earlier) and Scenes 1 to 3 (lite model, lab parallel 1) passed before the last three changes.
- The regression path (halt, rollback, verify_recovery) is still only unit-tested.
- Drift items C4 to C12 and the environment items in `docs/architecture.md` §12 are unchanged.

## Stray file `0` in the repo root: cause found, not fixed by me
`scripts/demo-reset.sh` line 121: `awk ... 'BEGIN{print (f+0)<=0 || (p+0)<=0 || (r+0)>0}'`. In awk, `print expr > x` is an output redirect, so the last `> 0` writes the result to a file named `0` in the current directory (every `make demo-reset`), and the command substitution gets no output, so the per-device health check `... == 1` can never be true: **the check never fails**. Fix (one line, `scripts/` is Codex's, so not applied): wrap the expression, `print ((f+0)<=0 || (p+0)<=0 || (r+0)>0)`. Delete `0` after each reset until then; I never commit it.

## Process notes
- Codex edits `infra/`, `scripts/`, `Makefile`, `docs/architecture.md`, `docs/aws-deploy.md` in this checkout; I committed only my own paths. During my last runs `demo-reset.sh` was briefly broken by an in-progress edit of theirs (`eep: command not found`); it worked again after.
- Model keys (`GEMINI_API_KEY`, `TFY_API_KEY`) are in the git-ignored `agent/spike/.env`; nothing secret is in any commit. The local TrueForge still has leftover providers from tests (`openai`, `truefoundry`); harmless, there is no delete route.

## What you must do by hand
1. If Scene 2 stalls in the demo, type `proceed`.
2. Apply the one-line awk fix in `scripts/demo-reset.sh` (or tell Codex).
3. Keep `agent/spike/.env` on AWS with `TFY_BASE_URL`, `TFY_API_KEY`, `TFY_MODEL`, `TFY_MODEL_DEV`, then `python3 agent/register.py --provider gateway --tier demo --register-provider`.
