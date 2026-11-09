#!/usr/bin/env bash
# Start the scrubber and contrarian validators (Claude Code, Haiku) in a new herdr tab with two panes.
# Run from a herdr-managed pane in WSL:  bash tools/herdr_validators.sh [--loop 60m]
# Agents write proposals/verdicts under validation/ only; read validation/summary.md for results.
set -euo pipefail
LOOP="60m"
if [ "${1:-}" = "--loop" ]; then LOOP="${2:?usage: --loop 60m}"; fi
test "${HERDR_ENV:-}" = 1 || { echo "not inside a herdr pane (HERDR_ENV != 1)"; exit 1; }
command -v jq >/dev/null || { echo "jq required"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$REPO/validation"
touch "$REPO/validation/summary.md" "$REPO/validation/scrubber_proposals.jsonl" "$REPO/validation/contrarian_verdicts.jsonl"

TOOLS='Read,Grep,Glob,WebFetch,Edit,Write,Bash(curl *),Bash(../.venv/Scripts/python.exe *),Bash(python3 *),Bash(jq *),Bash(zcat *),Bash(rtk *)'

tab=$(herdr tab create --workspace "$HERDR_WORKSPACE_ID" --cwd "$REPO" --label validators --no-focus)
p1=$(jq -r '.result.root_pane.pane_id' <<<"$tab")
p2=$(herdr pane split "$p1" --direction right --cwd "$REPO" --no-focus | jq -r '.result.pane.pane_id')

herdr agent start scrubber --kind claude --pane "$p1" -- --agent scrubber --model haiku \
  --permission-mode auto --allowedTools "$TOOLS"
herdr agent start contrarian --kind claude --pane "$p2" -- --agent contrarian --model haiku \
  --permission-mode auto --allowedTools "$TOOLS"

herdr agent prompt scrubber "/loop $LOOP Work your queue (S1-S4) per your agent instructions, one pass, then append to validation/summary.md."
herdr agent prompt contrarian "/loop $LOOP Work your queue (C1-C2) per your agent instructions, one pass, then append to validation/summary.md."
echo "validators started: scrubber=$p1 contrarian=$p2 (tab $(jq -r '.result.tab.tab_id' <<<"$tab"))"
