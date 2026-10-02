"""DeepPurpose CNN-CNN BindingDB의 CPU 로딩과 단일 DTA 추론을 검증한다."""

import hashlib
import json
import math
import os
import platform
import resource
import sys
from contextlib import redirect_stdout
from importlib.metadata import version
from numbers import Real
from pathlib import Path
from time import perf_counter

MODEL_ID = "CNN_CNN_BindingDB"
MODEL_DIR = Path("/opt/deeppurpose/model_cnn_cnn_bindingdb")
MODEL_ARCHIVE_DATAFILE_ID = "4159715"
MODEL_ARCHIVE_SHA256 = (
    "1f5c62863303d5057566b3b29be24e31cc138b361dfb8b085b53c8169d8c6829"
)
MPNN_MODEL_ID = "MPNN_CNN_BindingDB"
MPNN_MODEL_DIR = Path("/opt/deeppurpose/model_mpnn_cnn_bindingdb")
MPNN_MODEL_ARCHIVE_DATAFILE_ID = "4204178"
MPNN_MODEL_ARCHIVE_SHA256 = (
    "655119c3896a773a8ccf0e711ad263a3bfbcd0bab7ea5085cccb12e13908bc0c"
)
DEEPPURPOSE_SDIST_SHA256 = (
    "a912300c004954d3b8a32d8ca7cec1ebfe021bfa11dbb7f1e653ebe00c124094"
)

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
SARS_COV_2_MPRO = (
    "SGFRKMAFPSGKVEGCMVQVTCGTTTLNGLWLDDVVYCPRHVICTSEDMLNPNYEDLLIRKSNHNFLVQAG"
    "NVQLRVIGHSMQNCVLKLKVDTANPKTPKYKFVRIQPGQTFSVLACYNGSPSGVYQCAMRPNFTIKGSFLNG"
    "SCGSVGFNIDYDCVSFCYMHHMELPTGVHAGTDLEGNFYGPFVDRQTAQAAGTDTTITVNVLAWLYAAVING"
    "DRWFLNRFTTTLNDFNLVAMKYNYEPLTQDHVDILGPLSAQTGIAVLDMCASLKELLQNGMNGRTILGSALL"
    "EDEFTPFDVVRQCSGVTFQ"
)


def validate_prediction(raw: object) -> float:
    """단일 비불리언 유한 실수만 성공으로 인정한다."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 1:
        raise ValueError("Expected exactly one prediction")
    value = raw[0]
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("Prediction must be numeric")
    if not math.isfinite(value):
        raise ValueError("Prediction must be finite")
    return float(value)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selected_packages() -> dict[str, str]:
    names = [
        "DeepPurpose",
        "descriptastorus",
        "numpy",
        "pandas",
        "rdkit-pypi",
        "scikit-learn",
        "scipy",
        "torch",
    ]
    return {name: version(name) for name in names}


def main() -> None:
    if version("DeepPurpose") != "0.1.5":
        raise RuntimeError("This baseline requires DeepPurpose 0.1.5")
    expected_files = [MODEL_DIR / "config.pkl", MODEL_DIR / "model.pt"]
    if not all(path.is_file() for path in expected_files):
        raise RuntimeError("Pinned DeepPurpose checkpoint is missing")

    started = perf_counter()
    with redirect_stdout(sys.stderr):
        import torch
        from DeepPurpose import DTI
        from DeepPurpose.utils import data_process_repurpose_virtual_screening

        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        imported = perf_counter()
        model = DTI.model_pretrained(path_dir=str(MODEL_DIR))
        model.model.eval()
        loaded = perf_counter()
        data = data_process_repurpose_virtual_screening(
            [ASPIRIN],
            SARS_COV_2_MPRO,
            model.drug_encoding,
            model.target_encoding,
            "repurposing",
        )
        raw_prediction = model.predict(data)
        predicted = perf_counter()

    value = validate_prediction(raw_prediction)
    report = {
        "status": "passed",
        "scope": "installation_load_single_inference_only",
        "provider": "deeppurpose",
        "model": {
            "id": MODEL_ID,
            "drug_encoding": model.drug_encoding,
            "target_encoding": model.target_encoding,
            "training_dataset": "BindingDB",
            "training_endpoint": "Kd",
            "package_artifact_sha256": DEEPPURPOSE_SDIST_SHA256,
            "checkpoint_datafile_id": MODEL_ARCHIVE_DATAFILE_ID,
            "checkpoint_archive_sha256": MODEL_ARCHIVE_SHA256,
            "checkpoint_files_sha256": {
                path.name: sha256(path) for path in expected_files
            },
        },
        "input": {
            "smiles": ASPIRIN,
            "target_name": "SARS-CoV-2 main protease",
            "target_sequence": SARS_COV_2_MPRO,
            "target_sequence_sha256": hashlib.sha256(
                SARS_COV_2_MPRO.encode("ascii")
            ).hexdigest(),
        },
        "prediction": {
            "value": value,
            "score_type": "predicted_pkd",
            "unit": "-log10(Kd [M])",
        },
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "device": str(model.device),
            "logical_cpu_count": os.cpu_count(),
            "torch_threads": torch.get_num_threads(),
            "import_seconds": imported - started,
            "load_seconds": loaded - imported,
            "inference_seconds": predicted - loaded,
            "total_seconds": predicted - started,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        },
        "packages": selected_packages(),
        "limitations": [
            "Smoke test only; not predictive accuracy or clinical validation.",
            "The checkpoint has no machine-readable model card or split metadata.",
            "The pKd interpretation follows the upstream BindingDB Kd log-scale documentation.",
            "This old pretrained model may not generalize to unseen proteins.",
        ],
    }
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
