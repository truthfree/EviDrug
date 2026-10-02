"""ADMET endpoint catalog와 실행별 예측 결과의 정규화 계약."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from evidrug_api.execution_contracts.common import ContractModel

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class AdmetTaskType(StrEnum):
    """ADMET-AI endpoint의 학습 task 종류."""

    CLASSIFICATION = "classification"
    REGRESSION = "regression"


class AdmetEndpointDefinition(ContractModel):
    """모델 버전에 고정되어 실행마다 반복 저장할 필요가 없는 endpoint metadata."""

    endpoint_id: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=300)
    task_type: AdmetTaskType
    dataset_size: int | None = Field(default=None, ge=0)
    units: str | None = Field(default=None, max_length=120)
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)
    minimum_unbounded: bool = False
    maximum_unbounded: bool = False
    species: str | None = Field(default=None, max_length=200)
    tdc_rank: int | None = Field(default=None, ge=1)
    auprc: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    auroc: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    r_squared: float | None = Field(default=None, allow_inf_nan=False)
    mae: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    source_url: str | None = Field(default=None, max_length=1000, pattern=r"^https://")

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        """유한 범위와 unbounded 표식을 모순 없이 유지한다."""

        if self.minimum is not None and self.minimum_unbounded:
            raise ValueError("finite minimum must not be marked unbounded")
        if self.maximum is not None and self.maximum_unbounded:
            raise ValueError("finite maximum must not be marked unbounded")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self


class AdmetModelArtifact(ContractModel):
    """ADMET ensemble을 구성하는 모델 파일 식별자."""

    relative_path: str = Field(min_length=1, max_length=500)
    sha256: Sha256


class AdmetModelManifest(ContractModel):
    """여러 실행이 공유하는 모델·endpoint·percentile 기준 집단 manifest."""

    schema_version: Literal["1"] = "1"
    manifest_sha256: Sha256
    tool_id: Literal["admet_ai"] = "admet_ai"
    tool_version: str = Field(min_length=1, max_length=200)
    endpoint_metadata_sha256: Sha256
    reference_population: str = Field(min_length=1, max_length=300)
    reference_sha256: Sha256
    model_artifacts: tuple[AdmetModelArtifact, ...] = Field(min_length=1)
    endpoints: tuple[AdmetEndpointDefinition, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_unique_identifiers(self) -> Self:
        """catalog key와 모델 경로가 한 manifest 안에서 중복되지 않게 한다."""

        endpoint_ids = [endpoint.endpoint_id for endpoint in self.endpoints]
        artifact_paths = [artifact.relative_path for artifact in self.model_artifacts]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("endpoint definitions must have unique endpoint_id values")
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("model artifacts must have unique relative_path values")
        if any(not limitation.strip() for limitation in self.limitations):
            raise ValueError("limitations must not contain blank values")
        return self


class AdmetToolArguments(ContractModel):
    """고정 실행과 Agent function tool이 공유하는 최소 입력."""

    canonical_smiles: str = Field(min_length=1, max_length=5000)


class AdmetEndpointPrediction(ContractModel):
    """한 실행에서 생성된 endpoint 값과 기준 집단 내 percentile."""

    endpoint_id: str = Field(min_length=1, max_length=160)
    value: float = Field(allow_inf_nan=False)
    drugbank_approved_percentile: float = Field(ge=0, le=100, allow_inf_nan=False)


class AdmetToolResult(ContractModel):
    """Agent와 DB에 전달하는 실행별 ADMET prediction 행 모음."""

    schema_version: Literal["1"] = "1"
    manifest_sha256: Sha256
    canonical_smiles: str = Field(min_length=1, max_length=5000)
    predictions: tuple[AdmetEndpointPrediction, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_predictions(self) -> Self:
        """한 endpoint가 같은 실행 결과에 두 번 저장되지 않게 한다."""

        endpoint_ids = [prediction.endpoint_id for prediction in self.predictions]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("predictions must have unique endpoint_id values")
        return self


class AdmetNormalizedOutput(ContractModel):
    """외부 provider report를 정규화한 stable manifest와 per-run result."""

    manifest: AdmetModelManifest
    result: AdmetToolResult

    @model_validator(mode="after")
    def validate_manifest_reference(self) -> Self:
        """실행 결과가 같은 bundle의 manifest와 모든 endpoint를 정확히 참조하게 한다."""

        if self.result.manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("result must reference the included manifest")
        catalog_ids = {endpoint.endpoint_id for endpoint in self.manifest.endpoints}
        prediction_ids = {prediction.endpoint_id for prediction in self.result.predictions}
        if prediction_ids != catalog_ids:
            raise ValueError("predictions must match manifest endpoints exactly")
        return self
