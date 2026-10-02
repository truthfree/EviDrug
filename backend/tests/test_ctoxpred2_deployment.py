import hashlib
from pathlib import Path

import pytest

from evidrug_api.ctoxpred2 import deployment


def _installation(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    model = root / "artifacts" / "random_forest/hERG/model.joblib"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"reviewed-model")
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    (root / "runtime.py").write_text("# fixed runtime\n")
    monkeypatch.setattr(
        deployment,
        "CTOX_ARTIFACTS",
        {"random_forest/hERG/model.joblib": hashlib.sha256(b"reviewed-model").hexdigest()},
    )
    return model


def test_verified_installation_accepts_exact_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installation(tmp_path, monkeypatch)
    deployment.verify_ctox_installation(tmp_path)


def test_missing_runtime_refuses_recall(tmp_path: Path) -> None:
    with pytest.raises(deployment.CtoxDeploymentError, match="ctoxpred2_runtime_missing"):
        deployment.verify_ctox_installation(tmp_path)


def test_missing_or_corrupt_artifact_refuses_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _installation(tmp_path, monkeypatch)
    model.write_bytes(b"different-model")
    with pytest.raises(deployment.CtoxDeploymentError, match="ctoxpred2_artifact_hash_mismatch"):
        deployment.verify_ctox_installation(tmp_path)

    model.unlink()
    with pytest.raises(deployment.CtoxDeploymentError, match="ctoxpred2_artifact_missing"):
        deployment.verify_ctox_installation(tmp_path)
