"""PoC worker의 고정 CToxPred2 설치를 활성화 전에 검증한다."""

import hashlib
import os
from pathlib import Path

from evidrug_api.ctoxpred2.contracts import CTOX_ARTIFACTS

CTOX_RUNTIME_ROOT = Path("/opt/ctoxpred2")


class CtoxDeploymentError(RuntimeError):
    """고정 코드만 노출하는 배포 자산 오류."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_ctox_installation(root: Path = CTOX_RUNTIME_ROOT) -> None:
    """실행 파일과 네 artifact를 검증하고 실패 시 recall 시작을 거부한다."""
    python = root / ".venv/bin/python"
    runtime = root / "runtime.py"
    if not python.is_file() or not os.access(python, os.X_OK) or not runtime.is_file():
        raise CtoxDeploymentError("ctoxpred2_runtime_missing")
    for relative_path, expected in CTOX_ARTIFACTS.items():
        path = root / "artifacts" / relative_path
        if not path.is_file():
            raise CtoxDeploymentError("ctoxpred2_artifact_missing")
        try:
            actual = _sha256(path)
        except OSError as error:
            raise CtoxDeploymentError("ctoxpred2_artifact_unreadable") from error
        if actual != expected:
            raise CtoxDeploymentError("ctoxpred2_artifact_hash_mismatch")
