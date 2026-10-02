"""비밀 설정 없이 고정된 공개 분자의 모델 로딩·추론과 산출물 식별자를 검증한다."""

import csv
import hashlib
import json
import math
import platform
import sys
from collections.abc import Mapping
from contextlib import redirect_stdout
from importlib.metadata import distributions, version
from numbers import Real
from pathlib import Path
from time import perf_counter

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"


def validate_predictions(raw: object, expected: set[str]) -> dict[str, float]:
    """전체 항목을 보존하고 결측·비수치·비유한 값을 실패로 처리한다."""
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("Expected a non-empty prediction mapping")
    if not expected or not expected.issubset(raw):
        raise ValueError("Required model endpoints are missing")
    predictions = {}
    for name, value in raw.items():
        if not isinstance(name, str) or isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("Unexpected prediction type")
        if not math.isfinite(value):
            raise ValueError(f"Non-finite prediction: {name}")
        predictions[name] = float(value)
    return predictions


def sha256(path: Path) -> str:
    """모델과 참조 데이터의 실제 내용을 식별한다."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def main() -> None:
    """성공할 때만 JSON을 출력하며 실패 시 대체 예측 없이 비제로 종료한다."""
    if version("admet-ai") != "1.4.0":
        raise RuntimeError("This baseline requires ADMET-AI 1.4.0")
    started = perf_counter()
    # 라이브러리의 진행 출력을 결과 JSON과 분리한다.
    with redirect_stdout(sys.stderr):
        from admet_ai import ADMETModel
        from admet_ai.constants import (
            DEFAULT_ADMET_PATH,
            DEFAULT_DRUGBANK_PATH,
            DEFAULT_MODELS_DIR,
        )

        model = ADMETModel(num_workers=0, cache_molecules=False)
        loaded = perf_counter()
        expected = {name for tasks in model.task_lists for name in tasks}
        raw = model.predict(smiles=ASPIRIN)
        predicted = perf_counter()

    predictions = validate_predictions(raw, expected)
    weights = sorted(DEFAULT_MODELS_DIR.rglob("*.pt"))
    if not weights:
        raise RuntimeError("No model weights found")
    with DEFAULT_ADMET_PATH.open(newline="") as handle:
        endpoint_metadata = list(csv.DictReader(handle))
    report = {
        "status": "passed",
        "scope": "installation_load_single_inference_only",
        "smiles": ASPIRIN,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "device": str(model.device),
        "reference_population": "bundled DrugBank approved, all ATC groups",
        "load_seconds": loaded - started,
        "inference_seconds": predicted - loaded,
        "packages": dict(sorted((d.metadata["Name"], d.version) for d in distributions())),
        "model_sha256": {
            str(path.relative_to(DEFAULT_MODELS_DIR)): sha256(path) for path in weights
        },
        "reference_sha256": sha256(DEFAULT_DRUGBANK_PATH),
        "endpoint_metadata_sha256": sha256(DEFAULT_ADMET_PATH),
        "endpoint_metadata": endpoint_metadata,
        "predictions": predictions,
        "limitations": [
            "Smoke test only; not predictive accuracy or clinical validation.",
            "DrugBank percentiles are not prediction confidence.",
        ],
    }
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
