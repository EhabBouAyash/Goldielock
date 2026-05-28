#!/bin/bash
set -euo pipefail
ROOT="/Users/ehab/work/solo_projects"
cd "$ROOT"
mkdir -p "$ROOT/logs"
export TZ="America/New_York"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" "$ROOT/generate_insights.py"
fi
exec python3 "$ROOT/generate_insights.py"
