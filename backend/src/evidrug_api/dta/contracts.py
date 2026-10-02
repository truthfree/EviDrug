"""서로 다른 DTA 모델의 점수 의미와 실패 상태를 보존하는 계약."""

import hashlib
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from evidrug_api.execution_contracts.common import ContractModel

NonBlank = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class DtaArguments(ContractModel):
    """upstream에서 정규화한 분자와 실제 단백질 서열을 전달한다."""

    canonical_smiles: str = Field(min_length=1, max_length=4096, pattern=r"\S")
    target_sequence: str = Field(
        min_length=1, max_length=50000, pattern=r"^[ACDEFGHIKLMNPQRSTVWY]+$"
    )

    @property
    def target_sequence_sha256(self) -> str:
        return hashlib.sha256(self.target_sequence.encode("ascii")).hexdigest()


class DtaModel(ContractModel):
    """실행 간 공유하는 모델 출처. artifact hash는 실제 파일 또는 묶음의 hash다."""

    provider: NonBlank
    model_id: NonBlank
    version: NonBlank
    artifact_sha256: Sha256 | None = None


class DtaScoreType(StrEnum):
    PREDICTED_PKD = "predicted_pkd"
    PIC50_LIKE = "pic50_like"
    BINDING_PROBABILITY = "binding_probability"


SCORE_UNITS = {
    DtaScoreType.PREDICTED_PKD: "-log10(Kd [M])",
    DtaScoreType.PIC50_LIKE: "-log10(IC50 [M])",
    DtaScoreType.BINDING_PROBABILITY: "probability",
}


class DtaObservation(ContractModel):
    """score type이 다른 관측값은 평균하거나 공통 confidence로 변환하지 않는다."""

    score_type: DtaScoreType
    value: float = Field(strict=True, allow_inf_nan=False)
    unit: str

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        if self.unit != SCORE_UNITS[self.score_type]:
            raise ValueError("unit does not match score type")
        if self.score_type == DtaScoreType.BINDING_PROBABILITY and not 0 <= self.value <= 1:
            raise ValueError("binding probability must be between zero and one")
        return self


class DtaResult(ContractModel):
    """unavailable은 근거 부재이며 결합하지 않는다는 예측이 아니다."""

    model: DtaModel
    arguments: DtaArguments
    status: Literal["succeeded", "unavailable"]
    observations: tuple[DtaObservation, ...] = ()
    error_code: Literal["provider_timeout", "provider_unavailable"] | None = None
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status == "succeeded":
            if not self.observations or self.error_code is not None:
                raise ValueError("successful result requires observations and no error")
        elif self.observations or self.error_code is None:
            raise ValueError("unavailable result requires an error and no observations")
        keys = [item.score_type for item in self.observations]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate score types are not allowed")
        return self
