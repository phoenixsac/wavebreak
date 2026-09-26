#!/usr/bin/env bash
# Provision an Ubuntu 24.04 host and start the Wavebreak full container demo.
# Optional environment: REPO_URL, REPO_DIR, GIT_REF, WAVEBREAK_ENV_FILE, PUBLIC_HOST.
set -Eeuo pipefail

log() { printf '[wavebreak-install] %s\n' "$*"; }
die() { printf '[wavebreak-install] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die 'run as root (for example: sudo ./infra/aws/install.sh)'
[[ -r /etc/os-release ]] || die 'cannot identify operating system'
# shellcheck disable=SC1091
. /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || die "Ubuntu 24.04 required (found ${PRETTY_NAME:-unknown})"
[[ $(dpkg --print-architecture) == amd64 ]] || die 'the supported AWS target is amd64 (for example, m7i.2xlarge)'

REPO_URL=${REPO_URL:-https://github.com/phoenixsac/wavebreak.git}
GIT_REF=${GIT_REF:-master}
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SOURCE_REPO=$(git -C "$SCRIPT_DIR/../.." rev-parse --show-toplevel 2>/dev/null || true)
REPO_DIR=${REPO_DIR:-${SOURCE_REPO:-/opt/wavebreak}}

log 'installing Docker Engine, Compose, Node.js 22, and project tools'
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl gnupg git make jq socat ripgrep \
  python3 python3-yaml python3-venv python3-pip

if ! dpkg-query -W -f='${db:Status-Status}' docker-ce 2>/dev/null | grep -qx installed; then
  conflicting_packages=()
  for package in docker.io docker-compose docker-compose-v2 docker-doc \
    docker-buildx podman-docker containerd runc; do
    if dpkg-query -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null | grep -q '^ii'; then
      conflicting_packages+=("$package")
    fi
  done
  if ((${#conflicting_packages[@]})); then
    log "removing conflicting Docker packages: ${conflicting_packages[*]}"
    apt-get remove -y "${conflicting_packages[@]}"
  fi
fi

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc.tmp
chmod a+r /etc/apt/keyrings/docker.asc.tmp
mv /etc/apt/keyrings/docker.asc.tmp /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources.tmp <<DOCKER_REPO
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
DOCKER_REPO
mv /etc/apt/sources.list.d/docker.sources.tmp /etc/apt/sources.list.d/docker.sources

curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key |
  gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
chmod a+r /etc/apt/keyrings/nodesource.gpg
cat > /etc/apt/sources.list.d/nodesource.sources.tmp <<NODESOURCE_REPO
Types: deb
URIs: https://deb.nodesource.com/node_22.x
Suites: nodistro
Components: main
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/nodesource.gpg
NODESOURCE_REPO
mv /etc/apt/sources.list.d/nodesource.sources.tmp /etc/apt/sources.list.d/nodesource.sources

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin nodejs
systemctl enable --now docker

# Each systemd device container owns inotify instances; the Ubuntu default of
# 128 is too small for the full 20-device fleet plus host services.
printf 'fs.inotify.max_user_instances=1024\n' > /etc/sysctl.d/90-wavebreak-inotify.conf
sysctl --system >/dev/null

docker compose version >/dev/null
node -e 'const [major,minor]=process.versions.node.split(".").map(Number); if (major < 22 || (major === 22 && minor < 14)) process.exit(1)' \
  || die "Node.js >=22.14 is required by TrueForge (found $(node --version))"

log "preparing checkout at $REPO_DIR"
if [[ ! -e $REPO_DIR ]]; then
  install -d -m 0755 "$(dirname "$REPO_DIR")"
  git clone --branch "$GIT_REF" --single-branch "$REPO_URL" "$REPO_DIR"
elif [[ -d $REPO_DIR && -z $(find "$REPO_DIR" -mindepth 1 -maxdepth 1 -print -quit) ]]; then
  git clone --branch "$GIT_REF" --single-branch "$REPO_URL" "$REPO_DIR"
elif [[ -d $REPO_DIR/.git ]]; then
  [[ -f $REPO_DIR/Makefile && -f $REPO_DIR/platform/docker-compose.yml ]] \
    || die "$REPO_DIR is not a Wavebreak checkout"
  if [[ -n $(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" status --porcelain) ]]; then
    die "$REPO_DIR has local changes; commit or stash them before installation"
  fi
  origin=$(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" remote get-url origin)
  normalize_repo_url() { sed -E 's#^git@([^:]+):#https://\1/#; s#\.git$##; s#/$##' <<<"$1"; }
  [[ $(normalize_repo_url "$origin") == "$(normalize_repo_url "$REPO_URL")" ]] \
    || die "origin $origin does not match requested repository $REPO_URL"
  git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" fetch --prune origin "$GIT_REF"
  if ! git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" checkout "$GIT_REF" >/dev/null 2>&1; then
    git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" checkout -b "$GIT_REF" FETCH_HEAD
  fi
  git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" pull --ff-only origin "$GIT_REF"
elif [[ -d $REPO_DIR && -f $REPO_DIR/Makefile && -f $REPO_DIR/platform/docker-compose.yml ]]; then
  log 'using committed archive deployment already staged at REPO_DIR'
else
  die "$REPO_DIR exists but is not a git checkout; refusing to replace it"
fi

cd "$REPO_DIR"
if [[ -n ${WAVEBREAK_ENV_FILE:-} && ! -f .env ]]; then
  [[ -r $WAVEBREAK_ENV_FILE ]] || die "cannot read WAVEBREAK_ENV_FILE=$WAVEBREAK_ENV_FILE"
  install -m 0600 "$WAVEBREAK_ENV_FILE" .env
fi
[[ -f .env ]] || install -m 0600 .env.example .env
chmod 0600 .env

# Keep existing operator secrets. Replace only the checked-in example credentials/tokens.
PUBLIC_HOST=${PUBLIC_HOST:-}
if [[ -z $PUBLIC_HOST || $PUBLIC_HOST == localhost || $PUBLIC_HOST == 127.0.0.1 ]]; then
  IMDS_TOKEN=$(curl --noproxy '*' --connect-timeout 2 --max-time 3 -fsS -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    http://169.254.169.254/latest/api/token 2>/dev/null || true)
  if [[ -n $IMDS_TOKEN ]]; then
    PUBLIC_HOST=$(curl --noproxy '*' --connect-timeout 2 --max-time 3 -fsS \
      -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
      http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)
  fi
fi
WAVEBREAK_PUBLIC_HOST=${PUBLIC_HOST:-localhost} python3 - <<'PY'
import os
from pathlib import Path
import secrets
import tempfile

path = Path(".env")
lines = path.read_text().splitlines()
values = {}
for line in lines:
    if line and not line.lstrip().startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        values[key] = value

values["PROFILE"] = "full"
public_host = os.environ["WAVEBREAK_PUBLIC_HOST"]
if values.get("PUBLIC_HOST", "localhost") in {"", "localhost", "127.0.0.1"}:
    values["PUBLIC_HOST"] = public_host
for key in ("HAWKBIT_PASSWORD", "GRAFANA_ADMIN_PASSWORD"):
    if values.get(key, "") in {"", "admin"}:
        values[key] = secrets.token_urlsafe(32)
for key, placeholder in (
    ("HAWKBIT_LAB_PASSWORD", "lab"),
    ("HAWKBIT_GATEWAY_TOKEN", "change-me-gateway-token"),
    ("LAB_API_TOKEN", "change-me-lab-token"),
    ("LAB_DEVICE_TOKEN", "change-me-lab-device-token"),
):
    if values.get(key, "") in {"", placeholder}:
        values[key] = secrets.token_hex(32)

for key, default in (
    ("PROFILE", "full"),
    ("PUBLIC_HOST", "localhost"),
    ("HAWKBIT_PASSWORD", "admin"),
    ("GRAFANA_ADMIN_PASSWORD", "admin"),
    ("HAWKBIT_LAB_PASSWORD", "lab"),
    ("HAWKBIT_GATEWAY_TOKEN", "change-me-gateway-token"),
    ("LAB_API_TOKEN", "change-me-lab-token"),
    ("LAB_DEVICE_TOKEN", "change-me-lab-device-token"),
):
    values.setdefault(key, default)

output = []
written = set()
for line in lines:
    if line and not line.lstrip().startswith("#") and "=" in line:
        key = line.split("=", 1)[0]
        if key in values:
            output.append(f"{key}={values[key]}")
            written.add(key)
            continue
    output.append(line)
for key in values.keys() - written:
    output.append(f"{key}={values[key]}")

fd, temporary = tempfile.mkstemp(prefix=".env.", dir=".")
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(output) + "\n")
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
PUBLIC_HOST=$(sed -n 's/^PUBLIC_HOST=//p' .env | head -1)

log 'starting full container profile and publishing bundles'
make up PROFILE=full RUNTIME=container
make bundles publish
make fleet PROFILE=full RUNTIME=container
make demo-reset PROFILE=full
# Grafana keeps its initial admin password in its persistent volume. Keep it
# aligned when the generated or synced .env password changes between runs.
GRAFANA_PASSWORD=$(sed -n 's/^GRAFANA_ADMIN_PASSWORD=//p' .env | head -1)
docker compose --env-file .env -f platform/docker-compose.yml --profile full \
  exec -T grafana grafana cli admin reset-admin-password "$GRAFANA_PASSWORD" >/dev/null
make grafana-sa

# The images above were built from this source. Record their inputs so later
# aws-sync runs only rebuild when device or lab inputs change.
device_hash=$(python3 scripts/image-input-hash.py device)
lab_hash=$(python3 scripts/image-input-hash.py lab)
mkdir -p run
printf '%s %s\n' "$device_hash" "$lab_hash" > run/aws-sync-images.sha256
chmod 0600 run/aws-sync-images.sha256

log 'installing agent dependencies (agent/requirements.txt) into .venv'
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r agent/requirements.txt
.venv/bin/pip install --quiet PyYAML

# Gateway credentials are supplied out-of-band and never committed or printed.
[[ -f agent/spike/.env ]] || die 'agent/spike/.env missing; sync gateway settings before installing the agent services'
python3 - <<'PY'
from pathlib import Path

values = {}
for line in Path("agent/spike/.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        values[key] = value.strip().strip("'\"")
required = ("TFY_API_KEY", "TFY_MODEL", "TFY_MODEL_DEV")
missing = [name for name in required if not values.get(name)]
if missing or values.get("TFY_BASE_URL") != "https://gateway.truefoundry.ai":
    raise SystemExit("agent/spike/.env must define TFY_API_KEY, TFY_MODEL, TFY_MODEL_DEV, and TFY_BASE_URL=https://gateway.truefoundry.ai")
PY

wait_port() { # host port seconds
  local i
  for ((i = 0; i < $3; i++)); do
    (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

log 'installing persistent TrueForge and fleet MCP systemd services'
install -m 0644 infra/aws/trueforge.service /etc/systemd/system/wavebreak-trueforge.service
install -m 0644 infra/aws/fleet-mcp.service /etc/systemd/system/wavebreak-fleet-mcp.service
systemctl daemon-reload
systemctl enable --now wavebreak-trueforge.service wavebreak-fleet-mcp.service
wait_port 127.0.0.1 8790 120 || die 'TrueForge did not open :8790 (see agent/spike/trueforge.log)'
wait_port 127.0.0.1 8792 60 || die 'fleet MCP did not open :8792 (see run/fleet-mcp.log)'
if [[ -f agent/spike/.env ]]; then
.venv/bin/python agent/register.py --register-provider
else
  log 'WARNING: agent/spike/.env missing; add TrueFoundry gateway credentials, then register the agent'
fi

log 'demo status'
make demo-status
printf '\nWavebreak URLs (inbound access must be limited by the AWS security group):\n'
printf '  hawkBit API:       http://%s:8080\n' "$PUBLIC_HOST"
printf '  hawkBit UI:        http://%s:8081\n' "$PUBLIC_HOST"
printf '  Grafana:           http://%s:3000\n' "$PUBLIC_HOST"
printf '  mcp-grafana:       http://%s:8000/mcp\n' "$PUBLIC_HOST"
printf '  lab controller:    http://%s:8090\n' "$PUBLIC_HOST"
printf 'Secrets and generated credentials are in %s/.env (mode 0600, root-owned).\n' "$REPO_DIR"
