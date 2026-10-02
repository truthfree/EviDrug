#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# != 1 ]]; then
  echo "Usage: bash scripts/diagnose-dta.sh ANALYSIS_ID" >&2
  exit 2
fi
# Existing worker only: no build, restart, dotenv loading, or new analysis.
docker compose --env-file /dev/null -f compose.yaml exec -T worker \
  /app/.venv/bin/python - "$1" < scripts/diagnose_dta.py
