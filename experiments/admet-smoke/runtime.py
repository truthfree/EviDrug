"""ADMET-AI 1.4.0을 재사용하는 JSON-lines 모델 프로세스."""

import csv
import hashlib
import json
import re
import resource
import sys
from contextlib import redirect_stdout
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

from smoke import sha256, validate_predictions

ASSET_MANIFEST = Path(__file__).resolve().with_name("runtime-assets.json")


def asset_metadata():
    """잠금 패키지에 포함된 모델과 참조 데이터의 실제 hash를 수집한다."""
    from admet_ai.constants import DEFAULT_ADMET_PATH, DEFAULT_DRUGBANK_PATH, DEFAULT_MODELS_DIR

    weights = sorted(DEFAULT_MODELS_DIR.rglob("*.pt"))
    if not weights:
        raise RuntimeError("missing model weights")
    return {
        "model_sha256": {
            str(path.relative_to(DEFAULT_MODELS_DIR)): sha256(path) for path in weights
        },
        "reference_sha256": sha256(DEFAULT_DRUGBANK_PATH),
        "endpoint_metadata_sha256": sha256(DEFAULT_ADMET_PATH),
    }


class AdmetEngine:
    """첫 호출에만 모델과 고정 metadata를 로드한다."""

    def __init__(self):
        self.model = None
        self.metadata = None

    def predict(self, smiles):
        started = perf_counter()
        cold_start = self.model is None
        if cold_start:
            if version("admet-ai") != "1.4.0":
                raise RuntimeError("unsupported model version")
            import torch
            from admet_ai import ADMETModel
            from admet_ai.constants import DEFAULT_ADMET_PATH

            assets = asset_metadata()
            if assets != json.loads(ASSET_MANIFEST.read_text()):
                raise RuntimeError("model/reference assets changed since image build")
            torch.set_num_threads(1)
            torch.set_num_interop_threads(1)
            model = ADMETModel(num_workers=0, cache_molecules=False)
            if str(model.device) != "cpu":
                raise RuntimeError("CPU runtime required")
            with DEFAULT_ADMET_PATH.open(newline="") as handle:
                endpoints = list(csv.DictReader(handle))
            self.metadata = {
                **assets,
                "endpoint_metadata": endpoints,
                "packages": {"admet-ai": version("admet-ai"), "torch": version("torch")},
                "reference_population": "bundled DrugBank approved, all ATC groups",
                "limitations": [
                    "Not predictive accuracy or clinical validation.",
                    "DrugBank percentiles are not prediction confidence.",
                ],
            }
            self.model = model
        loaded = perf_counter()
        expected = {name for tasks in self.model.task_lists for name in tasks}
        predictions = validate_predictions(self.model.predict(smiles=smiles), expected)
        return {
            **self.metadata,
            "status": "passed",
            "smiles": smiles,
            "predictions": predictions,
        }, {
            "cold_start": cold_start,
            "load_seconds": loaded - started,
            "inference_seconds": perf_counter() - loaded,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        }


def serve(input_stream, output_stream, engine):
    """불완전·과대 입력은 추론 전에 거부하고 stdout은 protocol에만 사용한다."""
    while True:
        line = input_stream.readline(32769)
        if not line:
            return
        if len(line) > 32768 or not line.endswith(b"\n"):
            raise ValueError("invalid request line")
        request = json.loads(line)
        if not isinstance(request, dict) or set(request) != {"protocol", "request_id", "smiles"}:
            raise ValueError("invalid fields")
        if type(request["protocol"]) is not int or request["protocol"] != 1:
            raise ValueError("unsupported protocol")
        request_id, smiles = request["request_id"], request["smiles"]
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("invalid request id")
        if not isinstance(smiles, str) or not 1 <= len(smiles) <= 4096 or not smiles.strip():
            raise ValueError("invalid SMILES")
        with redirect_stdout(sys.stderr):
            report, metrics = engine.predict(smiles)
        reply = {
            "protocol": 1,
            "request_id": request_id,
            "input_sha256": hashlib.sha256(smiles.encode()).hexdigest(),
            "report": report,
            "runtime": metrics,
        }
        output_stream.write(json.dumps(reply, allow_nan=False) + "\n")
        output_stream.flush()


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--prepare-assets"]:
            # 빌드에서 uv.lock으로 검증한 패키지 내용을 기록한다. 새로운 upstream 서명은 아니다.
            ASSET_MANIFEST.write_text(json.dumps(asset_metadata(), sort_keys=True))
        elif sys.argv[1:]:
            raise ValueError("unexpected arguments")
        else:
            serve(sys.stdin.buffer, sys.stdout, AdmetEngine())
    except Exception:  # noqa: BLE001 - 모델 오류 원문에 입력이 포함될 수 있다.
        print("ADMET runtime failed", file=sys.stderr)
        raise SystemExit(1) from None
