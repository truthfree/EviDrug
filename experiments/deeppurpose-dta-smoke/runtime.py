"""Python 3.10 DeepPurpose 프로세스의 단일 요청/응답 JSON-lines 진입점."""

import argparse
import hashlib
import json
import re
import resource
import sys
from contextlib import redirect_stdout
from importlib.metadata import version
from time import perf_counter

from smoke import (
    MODEL_ARCHIVE_SHA256,
    MODEL_DIR,
    MODEL_ID,
    MPNN_MODEL_ARCHIVE_SHA256,
    MPNN_MODEL_DIR,
    MPNN_MODEL_ID,
    sha256,
    validate_prediction,
)

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 65536
MODELS = {
    MODEL_ID: {
        "directory": MODEL_DIR,
        "artifact_sha256": MODEL_ARCHIVE_SHA256,
        "checkpoint_hashes": {
            "config.pkl": "731dff45208f6e40c4d106b4a66e0bdf3cc44a0cef6e597b6b53ea8ffb3e153c",
            "model.pt": "318a402b72fa5e1c27244ffa78ab071813e4eb08522421bdd229d06f707ff723",
        },
    },
    MPNN_MODEL_ID: {
        "directory": MPNN_MODEL_DIR,
        "artifact_sha256": MPNN_MODEL_ARCHIVE_SHA256,
        "checkpoint_hashes": {
            "config.pkl": "a6371ee448684a04d34574f7a08c4d3e5716bbc4bf0c3b3da5b4f4eec4046429",
            "model.pt": "f0fb70eeca5ca4744be754ebbbb63affd52acc1e34ccb22c4024554c0b37b7bf",
        },
    },
}


def validate_request(request):
    """프로토콜 필드와 입력 한도를 모델 import 전에 검사한다."""
    if not isinstance(request, dict) or set(request) != {
        "protocol",
        "request_id",
        "arguments",
    }:
        raise ValueError("invalid request fields")
    if type(request["protocol"]) is not int or request["protocol"] != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol")
    if not isinstance(request["request_id"], str) or not re.fullmatch(
        r"[0-9a-f]{32}", request["request_id"]
    ):
        raise ValueError("invalid request id")
    arguments = request["arguments"]
    if not isinstance(arguments, dict) or set(arguments) != {
        "canonical_smiles",
        "target_sequence",
    }:
        raise ValueError("invalid arguments")
    smiles, sequence = arguments["canonical_smiles"], arguments["target_sequence"]
    if (
        not isinstance(smiles, str)
        or not 1 <= len(smiles) <= 4096
        or not smiles.strip()
    ):
        raise ValueError("invalid SMILES")
    if (
        not isinstance(sequence, str)
        or not 1 <= len(sequence) <= 50000
        or not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", sequence)
    ):
        raise ValueError("invalid sequence")
    return arguments


def arguments_hash(arguments):
    payload = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DeepPurposeEngine:
    """검증된 checkpoint를 첫 요청에 로드하고 동일 프로세스에서 재사용한다."""

    def __init__(self, model_id=MODEL_ID):
        selected = MODELS[model_id]
        self.metadata = {
            "provider": "deeppurpose",
            "model_id": model_id,
            "version": "0.1.5",
            "artifact_sha256": selected["artifact_sha256"],
        }
        self.model_directory = selected["directory"]
        self.checkpoint_hashes = selected["checkpoint_hashes"]
        self.model = None

    def predict(self, arguments):
        started = perf_counter()
        cold_start = self.model is None
        if cold_start:
            if version("DeepPurpose") != "0.1.5":
                raise RuntimeError("unsupported package version")
            # pickle 기반 config와 weights를 읽기 전에 두 파일의 고정 hash를 확인한다.
            for filename, expected in self.checkpoint_hashes.items():
                if sha256(self.model_directory / filename) != expected:
                    raise RuntimeError("checkpoint checksum mismatch")
            import torch
            from DeepPurpose import DTI

            torch.set_num_threads(1)
            torch.set_num_interop_threads(1)
            model = DTI.model_pretrained(path_dir=str(self.model_directory))
            model.config["num_workers"] = 0
            model.model.eval()
            if str(model.device) != "cpu":
                raise RuntimeError("CPU runtime required")
            self.model = model
        loaded = perf_counter()
        from DeepPurpose.utils import data_process_repurpose_virtual_screening
        from rdkit import Chem

        molecule = Chem.MolFromSmiles(arguments["canonical_smiles"])
        if molecule is None or molecule.GetNumAtoms() == 0:
            raise ValueError("invalid molecule")
        data = data_process_repurpose_virtual_screening(
            [arguments["canonical_smiles"]],
            arguments["target_sequence"],
            self.model.drug_encoding,
            self.model.target_encoding,
            "repurposing",
        )
        value = validate_prediction(self.model.predict(data))
        return value, {
            "cold_start": cold_start,
            "load_seconds": loaded - started,
            "inference_seconds": perf_counter() - loaded,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        }


def serve(input_stream, output_stream, engine):
    """stdout은 protocol 전용이다. 오류 시 종료하고 호출 측에 재시작을 맡긴다."""
    while True:
        line = input_stream.readline(MAX_LINE_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
            raise ValueError("request line exceeds limit or is incomplete")
        request = json.loads(line)
        arguments = validate_request(request)
        with redirect_stdout(sys.stderr):
            value, metrics = engine.predict(arguments)
        response = {
            "protocol": PROTOCOL_VERSION,
            "request_id": request["request_id"],
            "arguments_sha256": arguments_hash(arguments),
            "model": engine.metadata,
            "observations": [
                {
                    "score_type": "predicted_pkd",
                    "value": value,
                    "unit": "-log10(Kd [M])",
                }
            ],
            "runtime": metrics,
        }
        output_stream.write(json.dumps(response, allow_nan=False) + "\n")
        output_stream.flush()


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--model", choices=tuple(MODELS), required=True)
        args = parser.parse_args()
        serve(sys.stdin.buffer, sys.stdout, DeepPurposeEngine(args.model))
    except Exception:  # noqa: BLE001 - 프로세스 최상위에서 오류 원문 노출을 차단한다.
        # 라이브러리 예외에 분자·서열이 포함될 수 있으므로 원문은 출력하지 않는다.
        print("DeepPurpose runtime failed", file=sys.stderr)
        raise SystemExit(1) from None
