#!/usr/bin/env bash
# 사용자가 내용을 확인하고 실행한다. Codex의 소켓 제한은 변경하지 않는다.
set -euo pipefail

if [[ $# -ne 0 ]]; then
  echo '인자는 받지 않습니다. 실행 내용을 변경하면 파일을 다시 검토하세요.' >&2
  exit 2
fi

task_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
image_tag='evidrug-admet-smoke:1.4.0'
report_path="$task_dir/report.json"
temporary_report="$task_dir/report.json.tmp"

cleanup() {
  rm -f -- "$temporary_report"
}
trap cleanup EXIT

# build context는 이 전용 폴더뿐이다. Compose/.env/host mount는 사용하지 않는다.
docker build --platform linux/amd64 --tag "$image_tag" "$task_dir" >&2
docker run --rm --platform linux/amd64 \
  --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 128 \
  --memory 6g --cpus 2 --tmpfs /tmp:rw,nosuid,nodev,size=512m \
  "$image_tag" >"$temporary_report"

python3 - "$temporary_report" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text())
if report.get("status") != "passed":
    raise SystemExit("ADMET smoke report did not pass")
print("ADMET smoke test passed")
print(f"device: {report['device']}")
print(f"predictions: {len(report['predictions'])}")
print(f"model weights: {len(report['model_sha256'])}")
PY

mv -- "$temporary_report" "$report_path"
trap - EXIT
echo "full report: $report_path"
