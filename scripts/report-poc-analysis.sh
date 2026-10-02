#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# != 1 ]]; then
  echo "Usage: bash scripts/report-poc-analysis.sh ANALYSIS_ID" >&2
  exit 2
fi
# 사용자가 직접 실행한다. 기존 worker의 DB를 SELECT만 하며 Docker 서비스를 변경하지 않는다.
docker compose --env-file /dev/null -f compose.yaml exec -T worker \
  /app/.venv/bin/python - "$1" < scripts/report_poc_analysis.py
