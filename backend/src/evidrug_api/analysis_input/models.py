"""분석 입력 검증 API의 요청과 응답 모델."""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TargetMode(StrEnum):
    """사용자가 타깃을 지정했는지 나타낸다."""

    DISCOVER = "discover"
    SPECIFIED = "specified"


class PotencyEndpoint(StrEnum):
    """분석 시작 전에 고정할 수 있는 정량 결합 endpoint."""

    KD = "Kd"
    KI = "Ki"
    IC50 = "IC50"


class PotencyCriterion(BaseModel):
    """분석 도중 바꾸지 않는 compound-target potency 판단 기준."""

    model_config = ConfigDict(extra="forbid")

    endpoint: PotencyEndpoint
    maximum_value: float = Field(gt=0, allow_inf_nan=False)
    unit: str = Field(pattern=r"^(pM|nM|uM|mM|M)$")


class AnalysisInputRequest(BaseModel):
    """질환 확인 이후 분석을 준비하기 위해 받는 사용자 입력."""

    model_config = ConfigDict(extra="forbid")

    disease_id: str = Field(min_length=3, max_length=80)
    disease_name: str = Field(min_length=1, max_length=200)
    target_mode: TargetMode
    target_name: str | None = Field(default=None, max_length=200)
    smiles: str = Field(min_length=1, max_length=4096)
    potency_criterion: PotencyCriterion | None = None

    @model_validator(mode="after")
    def normalize_and_validate_target(self) -> Self:
        """공백을 정리하고 타깃 모드와 타깃명의 조합을 검증한다."""

        self.disease_id = self.disease_id.strip()
        self.disease_name = self.disease_name.strip()
        self.smiles = self.smiles.strip()
        self.target_name = self.target_name.strip() if self.target_name is not None else None

        if not self.disease_id:
            raise ValueError("disease_id must not be blank")
        if not self.disease_name:
            raise ValueError("disease_name must not be blank")
        if not self.smiles:
            raise ValueError("smiles must not be blank")

        if self.target_mode is TargetMode.SPECIFIED and not self.target_name:
            raise ValueError("target_name is required when target_mode is specified")
        if self.target_mode is TargetMode.DISCOVER and self.target_name:
            raise ValueError("target_name must not be provided when target_mode is discover")

        return self


class AnalysisInputResponse(BaseModel):
    """분석 단계가 안전하게 사용할 수 있도록 정리된 입력."""

    disease_id: str
    disease_name: str
    target_mode: TargetMode
    target_name: str | None
    original_smiles: str
    canonical_smiles: str
    potency_criterion: PotencyCriterion | None = None
