#!/usr/bin/env bash
# Ship only committed files; preserve host runtime state and Docker volumes.
set -Eeuo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
AWS_HOST=${AWS_HOST:-ubuntu@65.0.29.75}
AWS_KEY=${AWS_KEY:-${HOME}/.ssh/wavebreak-key.pem}
AWS_REMOTE_REPO=/home/ubuntu/wavebreak
[[ -r $AWS_KEY ]] || { echo "aws-sync: SSH key is not readable: $AWS_KEY" >&2; exit 1; }
[[ -f .env && -f agent/spike/.env ]] || {
  echo 'aws-sync: requires local .env and agent/spike/.env; refusing to upload incomplete secrets' >&2
  exit 1
}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
archive="$tmp/wavebreak.tar.gz"
git archive --format=tar HEAD | gzip -n > "$archive"

SSH=(ssh -i "$AWS_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
SCP=(scp -q -i "$AWS_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
retry_remote() {
  local attempt
  for attempt in 1 2 3; do
    "$@" && return 0
    if (( attempt < 3 )); then sleep 2; fi
  done
  local current_ip
  current_ip=$(curl -fsS --max-time 5 https://checkip.amazonaws.com 2>/dev/null || printf 'unavailable')
  echo "aws-sync: remote connection failed after 3 attempts; current public IP: $current_ip" >&2
  exit 1
}

retry_remote "${SCP[@]}" "$archive" "$AWS_HOST:/tmp/wavebreak-archive.tar.gz"
retry_remote "${SCP[@]}" .env "$AWS_HOST:/tmp/wavebreak-env"
retry_remote "${SCP[@]}" agent/spike/.env "$AWS_HOST:/tmp/wavebreak-agent-env"
retry_remote "${SSH[@]}" "$AWS_HOST" sudo bash -s -- "$AWS_REMOTE_REPO" <<'REMOTE'
set -Eeuo pipefail
repo=$1
archive=/tmp/wavebreak-archive.tar.gz
env_file=/tmp/wavebreak-env
agent_env=/tmp/wavebreak-agent-env
trap 'rm -f /tmp/wavebreak-archive.tar.gz /tmp/wavebreak-env /tmp/wavebreak-agent-env' EXIT
[[ -s $archive && -s $env_file && -s $agent_env ]] || { echo 'aws-sync: uploaded archive or env file missing' >&2; exit 1; }
chmod 0600 "$env_file" "$agent_env"

python3 - "$repo" "$archive" "$env_file" "$agent_env" <<'PY'
from pathlib import Path
import os
import shutil
import sys
import tarfile
import tempfile

repo, archive, env_file, agent_env = map(Path, sys.argv[1:])
repo.parent.mkdir(parents=True, exist_ok=True)

def env_values(path):
    result = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, value = stripped.split("=", 1)
                result[key] = value.strip().strip("'\"")
    return result

previous_env = env_values(repo / ".env")
incoming_env = env_values(env_file)
preserve_defaults = {
    "PROFILE": {"lite"},
    "PUBLIC_HOST": {"", "localhost", "127.0.0.1"},
    "HAWKBIT_PASSWORD": {"", "admin"},
    "GRAFANA_ADMIN_PASSWORD": {"", "admin"},
    "HAWKBIT_LAB_PASSWORD": {"", "lab"},
    "HAWKBIT_GATEWAY_TOKEN": {"", "change-me-gateway-token"},
    "LAB_API_TOKEN": {"", "change-me-lab-token"},
    "LAB_DEVICE_TOKEN": {"", "change-me-lab-device-token"},
    "GRAFANA_SA_TOKEN": {""},
}
preserved = {
    key: previous_env[key]
    for key, defaults in preserve_defaults.items()
    if incoming_env.get(key, "") in defaults and previous_env.get(key, "") and previous_env[key] not in defaults
}
incoming_lines = env_file.read_text().splitlines()
if preserved:
    lines = []
    replaced = set()
    for line in incoming_lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            key = line.split("=", 1)[0]
            if key in preserved:
                lines.append(f"{key}={preserved[key]}")
                replaced.add(key)
                continue
        lines.append(line)
    lines.extend(f"{key}={value}" for key, value in preserved.items() if key not in replaced)
    incoming_lines = lines

with tempfile.TemporaryDirectory(prefix=".wavebreak-stage-", dir=repo.parent) as temp:
    stage = Path(temp) / "repo"
    stage.mkdir(mode=0o755)
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(stage, filter="data")
    if repo.exists():
        for name in ("run", "build", ".venv", "logs"):
            old = repo / name
            new = stage / name
            if old.exists() and not new.exists():
                shutil.copytree(old, new, symlinks=True)
    (stage / ".env").parent.mkdir(parents=True, exist_ok=True)
    staged_env = stage / ".env"
    staged_env.write_text("\n".join(incoming_lines) + "\n")
    os.chmod(staged_env, 0o600)
    target_agent_env = stage / "agent/spike/.env"
    target_agent_env.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(agent_env, target_agent_env)
    os.chmod(target_agent_env, 0o600)
    backup = repo.parent / (repo.name + ".aws-sync-old")
    if backup.exists():
        if not repo.exists():
            backup.rename(repo)
        else:
            shutil.rmtree(backup)
    if repo.exists():
        repo.rename(backup)
    stage.rename(repo)
    if backup.exists():
        shutil.rmtree(backup)
PY

install -m 0600 "$env_file" "$repo/.env"
install -m 0600 "$agent_env" "$repo/agent/spike/.env"
install -m 0644 "$repo/infra/aws/trueforge.service" /etc/systemd/system/wavebreak-trueforge.service
install -m 0644 "$repo/infra/aws/fleet-mcp.service" /etc/systemd/system/wavebreak-fleet-mcp.service

if ! command -v docker >/dev/null 2>&1; then
  echo 'aws-sync: committed source and secrets staged; Docker is not installed yet. Run infra/aws/install.sh next.'
  exit 0
fi

cd "$repo"
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r agent/requirements.txt
.venv/bin/pip install --quiet PyYAML

mkdir -p run
device_hash=$(tar -cf - sim/device sim/ota-agent sim/inference-app sim/bundles/v1.0 | sha256sum | cut -d' ' -f1)
lab_hash=$(tar -cf - lab/controller wavebreak_clients | sha256sum | cut -d' ' -f1)
marker=run/aws-sync-images.sha256
old_device= old_lab=
if [[ -f $marker ]]; then read -r old_device old_lab < "$marker" || true; fi
if [[ $device_hash != "$old_device" ]]; then
  docker build -f sim/device/Dockerfile -t wavebreak-device:local .
fi
if [[ $lab_hash != "$old_lab" ]]; then
  docker compose --env-file .env -f platform/docker-compose.yml --profile lab build lab-controller
fi
printf '%s %s\n' "$device_hash" "$lab_hash" > "$marker"
chmod 0600 "$marker"

systemctl daemon-reload
systemctl enable wavebreak-trueforge.service wavebreak-fleet-mcp.service
systemctl restart wavebreak-trueforge.service wavebreak-fleet-mcp.service
rm -f "$archive" "$env_file" "$agent_env"
echo 'aws-sync: committed source deployed; Python requirements refreshed, changed images rebuilt, agent services restarted.'
REMOTE
