#!/usr/bin/env bash
set -euo pipefail

ARCH="$(uname -m)"
FC_VERSION="${FC_VERSION:-}"
FORCE="${FORCE:-0}"
DRY_RUN=0

# Parse arguments
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
  esac
done

# Determine FC_VERSION if not set
if [[ -z "$FC_VERSION" ]]; then
  FC_VERSION="$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://github.com/firecracker-microvm/firecracker/releases/latest | xargs basename)"
fi

FC_BIN="$HOME/.local/bin/firecracker"
FC_SHARE="$HOME/.local/share/firecracker"
VMLINUX_LINK="$FC_SHARE/vmlinux"

# Fetch firecracker binary if needed
if [[ ! -f "$FC_BIN" ]] || ! "$FC_BIN" --version 2>/dev/null | grep -q "$FC_VERSION"; then
  echo "Fetching firecracker $FC_VERSION for $ARCH..."
  mkdir -p "$HOME/.local/bin"

  if [[ "$DRY_RUN" == "0" ]]; then
    curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz" | \
      tar xz -C /tmp/ && \
      cp "/tmp/release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH}" "$FC_BIN" && \
      chmod +x "$FC_BIN" && \
      rm -rf "/tmp/release-${FC_VERSION}-${ARCH}"
  fi
fi

# Fetch kernel if needed
if [[ "$FORCE" == "1" ]] || [[ ! -L "$VMLINUX_LINK" && ! -f "$VMLINUX_LINK" ]]; then
  echo "Fetching kernel for firecracker..."
  mkdir -p "$FC_SHARE"

  # Try FC_VERSION minor and decrement down to v1.10
  # Strip leading 'v' if present
  ver="${FC_VERSION#v}"
  major="${ver%%.*}"
  minor="${ver#"${major}".}"
  minor="${minor%%.*}"

  found=0
  for ci_minor in $(seq "$minor" -1 10); do
    ci="v${major}.${ci_minor}"

    if [[ "$DRY_RUN" == "1" ]]; then
      echo "Checking: https://s3.amazonaws.com/spec.ccfc.min/?prefix=firecracker-ci/${ci}/${ARCH}/vmlinux-6.1&list-type=2"
    fi

    keys="$(curl -fsSL "https://s3.amazonaws.com/spec.ccfc.min/?prefix=firecracker-ci/${ci}/${ARCH}/vmlinux-6.1&list-type=2" | \
      grep -oP '(?<=<Key>)[^<]+' | grep -v '\.config$' || true)"

    # Filter out debug and no-acpi if alternatives exist
    if echo "$keys" | grep -qv 'debug\|no-acpi'; then
      keys="$(echo "$keys" | grep -v 'debug\|no-acpi')"
    fi

    # Choose highest version
    chosen="$(echo "$keys" | sort -V | tail -1)"

    if [[ -n "$chosen" ]]; then
      if [[ "$DRY_RUN" == "1" ]]; then
        echo "Chosen: $chosen"
        echo "URL: https://s3.amazonaws.com/spec.ccfc.min/${chosen}"
      else
        filename="$(basename "$chosen")"
        echo "Downloading: $chosen"
        curl -fsSL "https://s3.amazonaws.com/spec.ccfc.min/${chosen}" -o "$FC_SHARE/$filename"
        ln -sf "$filename" "$VMLINUX_LINK"
      fi
      found=1
      break
    fi
  done

  if [[ "$found" == "0" ]]; then
    echo "ERROR: No kernel found in S3" >&2
    exit 1
  fi
fi

# Print summary
if [[ "$DRY_RUN" == "0" ]]; then
  echo "Firecracker: $FC_BIN"
  echo "Kernel: $VMLINUX_LINK"
  "$FC_BIN" --version | head -1
fi
