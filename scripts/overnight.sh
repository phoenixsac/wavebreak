#!/usr/bin/env bash
# Overnight build loop: repeatedly runs headless Claude Code on docs/prompts/overnight.md (PART B)
# until docs/IMPLEMENTATION.md says "STATUS: COMPLETE".
#
#   tmux new -s night
#   ./scripts/overnight.sh          # Ctrl-b d to detach; `tmux attach -t night` to check
#
# Env overrides: ORCH_MODEL (default opus), MAX_ITER (40), MAX_TURNS (400), STALL_LIMIT (3),
#                SLEEP_OK (60), SLEEP_LIMIT (1800), EFFORT (unset = CLI default)
# Flags verified against `claude --help` (v2.1.283): -p, --model, --effort, --output-format,
# --dangerously-skip-permissions. --max-turns is accepted but not listed in --help (verified by running).
set -uo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO" || exit 1

ORCH_MODEL=${ORCH_MODEL:-opus}
MAX_ITER=${MAX_ITER:-40}
MAX_TURNS=${MAX_TURNS:-400}
STALL_LIMIT=${STALL_LIMIT:-3}
SLEEP_OK=${SLEEP_OK:-60}
SLEEP_LIMIT=${SLEEP_LIMIT:-1800}
PROMPT="Continue per docs/prompts/overnight.md"
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/overnight.log"
TRACKER="$REPO/docs/IMPLEMENTATION.md"
LIMIT_RE='usage limit|limit reached|rate limit|rate_limit|limit will reset|out of extra usage|overloaded'

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "[$(ts)] $*" | tee -a "$LOG"; }

command -v claude >/dev/null || { echo "claude CLI not found" >&2; exit 1; }

extra=()
[ -n "${EFFORT:-}" ] && extra+=(--effort "$EFFORT")

stalls=0
for ((i = 1; i <= MAX_ITER; i++)); do
  if grep -q '^STATUS: COMPLETE' "$TRACKER"; then
    say "STATUS: COMPLETE found; stopping."
    exit 0
  fi

  head_before=$(git rev-parse HEAD)
  out=$(mktemp)
  say "=== run $i/$MAX_ITER start (model=$ORCH_MODEL, max-turns=$MAX_TURNS, HEAD=${head_before:0:7})"

  claude -p "$PROMPT" \
    --model "$ORCH_MODEL" \
    --dangerously-skip-permissions \
    --max-turns "$MAX_TURNS" \
    --output-format text \
    "${extra[@]}" < /dev/null 2>&1 | tee "$out" >> "$LOG"
  rc=${PIPESTATUS[0]}

  head_after=$(git rev-parse HEAD)
  commits=$(git rev-list --count "${head_before}..${head_after}")
  dirty=$(git status --porcelain | wc -l)
  say "=== run $i end: exit=$rc commits=$commits uncommitted_files=$dirty"

  if grep -qiE "$LIMIT_RE" "$out" && [ "$commits" -eq 0 ]; then
    rm -f "$out"
    say "usage/rate limit detected; sleeping ${SLEEP_LIMIT}s"
    sleep "$SLEEP_LIMIT"
    continue
  fi
  rm -f "$out"

  if [ "$commits" -eq 0 ]; then
    stalls=$((stalls + 1))
    say "no commits this run (stall $stalls/$STALL_LIMIT)"
    if [ "$stalls" -ge "$STALL_LIMIT" ]; then
      say "stopping: $STALL_LIMIT consecutive runs without commits; check $LOG"
      exit 3
    fi
  else
    stalls=0
  fi

  sleep "$SLEEP_OK"
done

say "hit MAX_ITER=$MAX_ITER; stopping."
exit 2
