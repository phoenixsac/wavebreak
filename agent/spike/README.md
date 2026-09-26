# agent/spike — TrueForge verification spike (throwaway)

Verifies TrueForge behaviour for `docs/agent-design.md` §21. Results are recorded there.

| File | Purpose |
|---|---|
| `start_trueforge.sh` | Start TrueForge local mode on :8790 (log `trueforge.log`) with the SSRF allowlist and raised MCP timeout |
| `spike_mcp.py` | Throwaway MCP server (streamable HTTP :8791): `get_status` (readOnly), `write_marker` (unannotated write), `wait_seconds` (blocking) |
| `drive.py` | HTTP + SSE helpers: create agent/session, run turn |
| `t_*.py` | Tests: approval, deny/default, timeout, sandbox probe (`probe.sh`), Code Mode, skill |

Setup: `python3 -m venv .venv && .venv/bin/pip install 'mcp<2' httpx uvicorn`; `.env` (git-ignored) holds `GEMINI_API_KEY`. Register the provider and MCP server via `PUT /api/v1/settings/...` (see design doc §21).
