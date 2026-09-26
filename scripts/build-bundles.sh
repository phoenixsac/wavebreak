#!/usr/bin/env bash
# Builds deterministic release tarballs: build/bundles/wavebreak-app-vX.Y.tar (+ .sha256).
# Tar root holds manifest.json, config.yaml, app/ (what ota-agent expects).
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
SRC="$REPO/sim/bundles"
OUT="$REPO/build/bundles"
mkdir -p "$OUT"

for dir in "$SRC"/v*/; do
  version=$(basename "$dir")
  tarball="$OUT/wavebreak-app-$version.tar"
  tar --sort=name --mtime='2026-01-01 00:00:00Z' --owner=0 --group=0 --numeric-owner \
      --exclude='__pycache__' --exclude='*.pyc' \
      -C "$dir" -cf "$tarball" manifest.json config.yaml app
  (cd "$OUT" && sha256sum "$(basename "$tarball")" > "$(basename "$tarball").sha256")
  echo "built $(cut -c1-16 "$tarball.sha256")…  $tarball"
done
