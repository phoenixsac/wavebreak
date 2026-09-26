# AWS deployment runbook

Target: the existing Ubuntu 24.04 instance `ubuntu@<EC2_PUBLIC_IP>` in Mumbai (`m7i.2xlarge`, 58 GB root disk). The security group should allow only TCP 22 from the operator's current public IP `/32`; keep application ports closed and use the SSH tunnel below.

## Prerequisites

From the WSL checkout, have these git-ignored files ready:

- `.env` with the backend credentials and settings.
- `agent/spike/.env` with `TFY_BASE_URL=https://gateway.truefoundry.ai`, `TFY_API_KEY`, `TFY_MODEL`, and `TFY_MODEL_DEV`.

Never commit either file. `make aws-sync` uploads them quietly with remote mode 0600. Set the target with `AWS_HOST=ubuntu@<EC2_PUBLIC_IP>` and `AWS_KEY=~/.ssh/<KEY>.pem` (environment variables read by `scripts/aws-sync.sh`; the script's built-in defaults are for the original deployment).

## First deployment

Commit the files to deploy, then run from the repository root:

```bash
make aws-sync
ssh -i ~/.ssh/<KEY>.pem ubuntu@<EC2_PUBLIC_IP>
sudo env REPO_DIR=/home/ubuntu/wavebreak bash /home/ubuntu/wavebreak/infra/aws/install.sh
```

The first sync stages only the committed archive and the two secret files. The installer provisions Docker Compose, Node.js 22, `socat`, `ripgrep`, and Python tooling; starts `PROFILE=full RUNTIME=container`; publishes bundles; launches the 20-device fleet; resets it to healthy v1.1; provisions Grafana's read-only account; and enables the two systemd services. The instance's `~/wavebreak/.env` is kept mode 0600. Full startup and registration can take several minutes.

The TrueForge service reads `agent/spike/.env`, listens on 8790, and sets `OUTBOUND_URL_ALLOWED_HOSTS` for loopback MCP access plus `MCP_REQUEST_TIMEOUT_MS=900000`. The fleet MCP service listens on 8792, reads the repo `.env`, and uses `FLEET_LAB_PARALLEL=2`. Both restart after failure and at boot. `socat`, `ripgrep` and `bubblewrap` are installed for TrueForge's local sandbox (the service runs as root, where `bwrap` works; on Ubuntu 24.04 unprivileged users are blocked by AppArmor).

## Update and registration workflow

1. Commit the intended code and docs. Uncommitted files are never part of the upload.
2. Push the commit to `origin` when it should be available to others.
3. Run `make aws-sync`. It uploads `git archive HEAD`, replaces deployed source while preserving `run/`, `build/`, `.venv`, logs, Docker images, named volumes, and hawkBit data. It copies both secret files with mode 0600; on subsequent syncs it retains AWS-generated `.env` credentials when the local file still has example defaults. It refreshes `agent/requirements.txt` and rebuilds device/lab images only when their inputs change, then restarts both agent services. It does not publish bundles or reset the fleet on every code sync.
4. If the agent manifest, instructions, provider configuration, or MCP registration changed, re-register against the running TrueForge:

   ```bash
   ssh -i ~/.ssh/<KEY>.pem ubuntu@<EC2_PUBLIC_IP> \
     'sudo /home/ubuntu/wavebreak/.venv/bin/python /home/ubuntu/wavebreak/agent/register.py --register-provider'
   ```

`register.py` reads gateway values from `agent/spike/.env`; it does not print the API key. Re-run registration after syncing a committed update to `agent/register.py`, `agent/wavebreak-agent.json`, or the MCP server definitions.

## Verify the deployment

Run these on the instance after startup or an update:

```bash
cd /home/ubuntu/wavebreak
sudo make demo-reset PROFILE=full
sudo make demo-status
sudo systemctl --no-pager --full status wavebreak-trueforge wavebreak-fleet-mcp
curl -fsS http://127.0.0.1:3000/api/health
curl -fsS http://127.0.0.1:8790/api/v1/agents | jq '.data[] | select(.name == "wavebreak")'
sudo .venv/bin/python agent/driver.py "Reply with exactly: gateway check passed. Do not use tools."
```

`demo-status` should report 20 healthy v1.1 devices (12 rev A, 8 rev B), no active rollouts, an empty lab, and service URLs. Confirm the Wavebreak Fleet dashboard has Prometheus and Loki data in Grafana. Confirm the agent is listed in TrueForge and run a harmless prompt such as “Summarize fleet status without changing anything” to verify a gateway turn. If hawkBit state has been lost, restore release sets and device assignments with `sudo make bundles publish PROFILE=full` followed by `sudo make seed PROFILE=full` and `sudo make demo-reset PROFILE=full`.

## WSL browser access

Keep the security group limited to SSH and run this tunnel in WSL:

```bash
ssh -i ~/.ssh/<KEY>.pem -N \
  -L 13000:127.0.0.1:3000 \
  -L 18790:127.0.0.1:8790 \
  -L 18080:127.0.0.1:8080 \
  -L 18081:127.0.0.1:8081 \
  ubuntu@<EC2_PUBLIC_IP>
```

Open Grafana at `http://localhost:13000` (user `admin`, password in the instance `.env`), TrueForge at `http://localhost:18790`, hawkBit API at `http://localhost:18080`, and hawkBit UI at `http://localhost:18081` (user `admin`, password in `.env`). The SSH tunnel is long-running; use a second WSL terminal for commands.

## Deployment log (2026-09-26)

Verified on `ubuntu@<EC2_PUBLIC_IP>` (`m7i.2xlarge`, 31 GB RAM):

- `make aws-sync` then `install.sh` re-run: the Grafana step now passes (the installer reads `GRAFANA_ADMIN_PASSWORD` from `.env`, commit `146f07b`); Viewer token stored, mcp-grafana restarted.
- The installer's agent registration first failed with `sandbox is enabled but no sandbox provider is configured`: `bubblewrap` was missing. Fix: `sudo apt-get install -y bubblewrap`, `sudo systemctl restart wavebreak-trueforge` (log line `Local sandbox fallback is available`), and `bubblewrap` is now in `install.sh`. Nothing else is needed for the sandbox (no Daytona key).
- Registered on the instance: `sudo /home/ubuntu/wavebreak/.venv/bin/python /home/ubuntu/wavebreak/agent/register.py --provider gateway --tier demo --register-provider` (provider `tfy-gateway`, both MCP servers, agent `wavebreak` on `openai-polaris/gpt-4o`). Test turn on the dev model (`openai-polaris/gpt-4.1-mini`, `--tier dev`): replied `ok`, 6,729 tokens, $0.0018; the agent was switched back to `--tier demo`.
- `sudo make demo-reset PROFILE=full` (run with `sudo`, because `.env` and `agent/spike/.env` are root-owned): 20 devices healthy on v1.1 (12 rev A, 8 rev B), lab empty. `sudo make demo-status PROFILE=full` shows all 20 healthy.
- Services: `wavebreak-trueforge` (127.0.0.1:8790) and `wavebreak-fleet-mcp` (127.0.0.1:8792) are active and restart on failure. Grafana 3000, hawkBit API 8080 and UI 8081 listen on all interfaces (reach them through the SSH tunnel; keep the security group at SSH only).
- The full-profile agent scenes were not run on AWS. Lab parallelism there is 2, so a rehearsal of two revisions should take about 3 minutes.

