"""Pinned CToxPred2 RF-SSL JSON-lines runtime.

Only load artifacts after their SHA-256 values match the reviewed manifest. The
artifact root is an administrator-controlled argv, never a model-generated value.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from mordred import Calculator, descriptors
from PyBioMed.PyMolecule.fingerprint import (
    CalculateECFP2Fingerprint,
    CalculatePubChemFingerprint,
)
from rdkit import Chem
from sklearn.base import BaseEstimator
from sklearn.feature_selection import SelectorMixin

UPSTREAM_COMMIT = "2a31aa119e27b6b69a5588d18a01f2a27fef4524"
PYBIOMED_COMMIT = "45440d8a70b2aa2818762ceadb499dd3a1df90bc"
ARTIFACTS = {
    "decriptors_preprocessing/global_preprocessing_pipeline.sav": (
        "58ca80d195ca2f261aa50e68e88a82aebdccdc106149f89d5bb9d10d54e7f301"
    ),
    "random_forest/hERG/_ssl_herg_model.joblib": (
        "1060d8afee2df325410e11fe5b1a6457656f158f63dc4baab0dc1ccc9a0a98c6"
    ),
    "random_forest/Nav1.5/_ssl_nav_model.joblib": (
        "0b57bc75c80518c1ec01a597f0fde3cbeb5a6a55f8f48d7afc26ea5b8ff30b8b"
    ),
    "random_forest/Cav1.2/_ssl_cav_model.joblib": (
        "d7be3bcb59890058208a18167246c395d5ac4ad5b9d762ff07b0b0643ac375e9"
    ),
}
PACKAGES = {
    "numpy": "1.23.5",
    "pandas": "2.0.3",
    "scipy": "1.11.4",
    "scikit-learn": "1.3.1",
    "joblib": "1.5.1",
    "rdkit": "2025.3.5",
    "mordred": "1.2.0",
    "pybiomed": "git-" + PYBIOMED_COMMIT,
}


class CorrelationThreshold(SelectorMixin, BaseEstimator):
    """Upstream preprocessing pickle compatibility class."""

    def __init__(self, threshold: float | None = None) -> None:
        self.threshold = threshold if threshold is not None else 1.0

    def fit(self, features: Any, target: Any = None) -> CorrelationThreshold:
        del target
        correlation = np.abs(np.corrcoef(features, rowvar=False))
        self.mask = ~(np.tril(correlation, k=-1) > self.threshold).any(axis=1)
        return self

    def _get_support_mask(self) -> Any:
        return self.mask


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _verify_environment(root: Path) -> dict[str, Path]:
    paths = {name: root / name for name in ARTIFACTS}
    for name, path in paths.items():
        if not path.is_file() or _digest(path) != ARTIFACTS[name]:
            raise RuntimeError("artifact verification failed: " + name)
    for name, expected in PACKAGES.items():
        if name == "pybiomed":
            continue
        if importlib.metadata.version(name) != expected:
            raise RuntimeError("package version mismatch: " + name)
    return paths


def _features(smiles: str, preprocessing: Any) -> np.ndarray:
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise ValueError("invalid canonical SMILES")
    ecfp2 = CalculateECFP2Fingerprint(molecule)[0]
    pubchem = CalculatePubChemFingerprint(molecule)
    fingerprints = np.concatenate((ecfp2, pubchem)).reshape(1, -1)
    frame = Calculator(descriptors, ignore_3D=True).pandas([molecule], nproc=1, quiet=True)
    transformed = preprocessing.transform(frame)
    return np.concatenate((fingerprints, transformed), axis=1)


def _prediction(model: Any, features: np.ndarray) -> dict[str, object]:
    probabilities = model.predict_proba(features)[0]
    index = int(np.argmax(probabilities))
    # Upstream uses argmax index as the binary class. Refuse reordered classes.
    if list(model.classes_) != [0, 1]:
        raise RuntimeError("unexpected classifier classes")
    return {"label": index, "class_probability": float(probabilities[index])}


def serve(root: Path) -> None:
    paths = _verify_environment(root)
    preprocessing = joblib.load(paths["decriptors_preprocessing/global_preprocessing_pipeline.sav"])
    models = {
        "hERG": joblib.load(paths["random_forest/hERG/_ssl_herg_model.joblib"]),
        "Nav1.5": joblib.load(paths["random_forest/Nav1.5/_ssl_nav_model.joblib"]),
        "Cav1.2": joblib.load(paths["random_forest/Cav1.2/_ssl_cav_model.joblib"]),
    }
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("protocol") != 1 or set(request) != {
            "protocol",
            "request_id",
            "arguments",
        }:
            raise ValueError("invalid request envelope")
        arguments = request["arguments"]
        if set(arguments) != {"schema_version", "canonical_smiles"}:
            raise ValueError("invalid request arguments")
        payload = json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        features = _features(arguments["canonical_smiles"], preprocessing)
        report = {
            "status": "passed",
            "canonical_smiles": arguments["canonical_smiles"],
            "upstream_repository": "issararab/CToxPred2",
            "upstream_commit": UPSTREAM_COMMIT,
            "profile": "rf_ssl",
            "packages": PACKAGES,
            "artifacts": ARTIFACTS,
            "predictions": {
                channel: _prediction(model, features) for channel, model in models.items()
            },
        }
        response = {
            "protocol": 1,
            "request_id": request["request_id"],
            "arguments_sha256": hashlib.sha256(payload).hexdigest(),
            "report": report,
        }
        print(json.dumps(response, separators=(",", ":")), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    root = args.artifact_root.resolve()
    if args.verify_only:
        _verify_environment(root)
        return
    serve(root)


if __name__ == "__main__":
    main()
