#!/bin/zsh
set -euo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "用法: $0 INPUT_FILE MAS_SUMMARY [MEETING_DATE]" >&2
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
INPUT_FILE=$1
MAS_SUMMARY=$2
MEETING_DATE=${3:-}

PYTHON_BIN=${INVESTMENT_MINUTES_PYTHON:-python3}
CMD=("$PYTHON_BIN" "$SCRIPT_DIR/export_to_obsidian.py" "$INPUT_FILE" --mas-summary "$MAS_SUMMARY")

if [ -n "$MEETING_DATE" ]; then
  CMD+=(--meeting-date "$MEETING_DATE")
fi

"${CMD[@]}"
