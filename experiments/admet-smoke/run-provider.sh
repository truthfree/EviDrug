#!/usr/bin/env bash
# 사용자가 Docker를 실행한다. .env, API key와 운영 DB를 전달하지 않는다.
set -euo pipefail
task_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$task_dir/../.." && pwd)"
backend_python="$project_dir/backend/.venv/bin/python"
profile="${1:-1c-2g}"
if [[ $# -gt 1 || ( "$profile" != "free" && "$profile" != "1c-2g" ) ]]; then
  echo '사용법: bash experiments/admet-smoke/run-provider.sh [free|1c-2g]' >&2
  exit 2
fi
if [[ ! -x "$backend_python" ]]; then
  echo '먼저 backend에서 uv sync --frozen --no-env-file을 실행하세요.' >&2
  exit 2
fi
docker build --platform linux/amd64 --tag evidrug-admet-smoke:1.4.0 "$task_dir" >&2
PYTHONPATH="$project_dir/backend/src" "$backend_python" "$task_dir/verify_provider.py" --profile "$profile"
