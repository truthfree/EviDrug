"""Read saved DTA input, run one isolated model diagnostic, never write SQL or call LLM."""

import json
import os
import subprocess
import sys
import time
from uuid import UUID

from sqlalchemy import create_engine, text

# Do not invoke runtime.__main__: its deliberately generic error hides the failing frame.
# Never print exception messages, locals, input sequences, credentials, or library logs.
CHILD = r'''
import contextlib, json, os, sys, traceback
sys.path.insert(0, "/opt/dta")
stage = "import"
try:
    with open(os.devnull, "w") as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            import runtime
            stage = "validate_input"
            request = json.load(sys.stdin)
            arguments = runtime.validate_request(request)
            stage = "load_or_predict"
            value, metrics = runtime.DeepPurposeEngine().predict(arguments)
    report = {"status": "passed", "value": value, "score_type": "predicted_pkd",
              "unit": "-log10(Kd [M])", "runtime": metrics}
except Exception as error:
    report = {"status": "failed", "stage": stage,
              "error_type": type(error).__name__, "errno": getattr(error, "errno", None),
              "frames": [{"file": os.path.basename(f.filename), "line": f.lineno,
                          "function": f.name} for f in traceback.extract_tb(error.__traceback__)]}
print(json.dumps(report, allow_nan=False), flush=True)
sys.exit(0 if report["status"] == "passed" else 1)
'''


def main():
    analysis_id = UUID(sys.argv[1])
    # Read only the database connection environment, never load Settings or .env.
    engine = create_engine(os.environ["EVIDRUG_DATABASE_URL"])
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            rows = connection.execute(
                text("SELECT tool_call_id, canonical_smiles, target_sequence "
                     "FROM dta_executions WHERE analysis_id=:analysis_id"),
                {"analysis_id": analysis_id},
            ).mappings().all()
    finally:
        engine.dispose()
    # Avoid silently rerunning every shortlist candidate.
    if len(rows) != 1:
        print(json.dumps({"status": "not_run", "reason": "expected_one_saved_dta_input",
                          "count": len(rows)}))
        return 2
    row = rows[0]
    request = {"protocol": 1, "request_id": "0" * 32, "arguments": {
        "canonical_smiles": row["canonical_smiles"], "target_sequence": row["target_sequence"]}}
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR")
                   if key in os.environ}
    environment.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
                       CUDA_VISIBLE_DEVICES="", PYTHONUNBUFFERED="1")
    started = time.monotonic()
    print(json.dumps({"status": "running", "analysis_id": str(analysis_id),
                      "source_tool_call_id": str(row["tool_call_id"]),
                      "llm_calls": 0, "sql_writes": 0}), flush=True)
    try:
        result = subprocess.run(
            ["/opt/dta/.venv/bin/python", "-c", CHILD], input=json.dumps(request),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment, timeout=300, check=False,
        )
        try:
            report = json.loads(result.stdout)
        except ValueError:
            report = {"status": "failed", "reason": "no_valid_diagnostic_reply"}
        report["process_returncode"] = result.returncode
    except subprocess.TimeoutExpired:
        report = {"status": "failed", "reason": "diagnostic_timeout_300s"}
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"status": "failed", "stage": "diagnostic_harness",
                          "error_type": type(error).__name__}))
        sys.exit(1)
