"""CToxPred2 채널별 예측과 모델 provenance의 typed 계약."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from evidrug_api.execution_contracts.common import ContractModel

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]

CTOX_UPSTREAM_COMMIT = "2a31aa119e27b6b69a5588d18a01f2a27fef4524"
CTOX_TOOL_VERSION = "rf-ssl-2a31aa119e27-r1"
CTOX_PACKAGES = {
    "numpy": "1.23.5",
    "pandas": "2.0.3",
    "scipy": "1.11.4",
    "scikit-learn": "1.3.1",
    "joblib": "1.5.1",
    "rdkit": "2025.3.5",
    "mordred": "1.2.0",
    "pybiomed": "git-45440d8a70b2aa2818762ceadb499dd3a1df90bc",
}
CTOX_ARTIFACTS = {
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


class CtoxChannel(StrEnum):
    """CToxPred2가 제공하는 세 심장 이온통로."""

    HERG = "herg"
    NAV1_5 = "nav1_5"
    CAV1_2 = "cav1_2"


class CtoxPredictionLabel(StrEnum):
    """모델의 1/0 출력을 임상 독성 확정과 구분한 라벨."""

    POSITIVE = "positive"
    NEGATIVE = "negative"


class CtoxModelArtifact(ContractModel):
    """고정 RF profile을 구성하는 preprocessing/model 파일."""

    relative_path: str = Field(min_length=1, max_length=500)
    sha256: Sha256


class CtoxModelIdentity(ContractModel):
    """같은 tool ID 아래 재현 가능한 upstream RF-SSL profile."""

    schema_version: Literal["1"] = "1"
    upstream_repository: Literal["issararab/CToxPred2"] = "issararab/CToxPred2"
    upstream_commit: GitCommit
    profile: Literal["rf_ssl"] = "rf_ssl"
    packages: dict[str, str] = Field(min_length=1)
    artifacts: tuple[CtoxModelArtifact, ...] = Field(min_length=4)

    @model_validator(mode="after")
    def validate_artifacts(self) -> Self:
        paths = [item.relative_path for item in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("CToxPred2 artifact paths must be unique")
        required_suffixes = {
            "global_preprocessing_pipeline.sav",
            "_ssl_herg_model.joblib",
            "_ssl_nav_model.joblib",
            "_ssl_cav_model.joblib",
        }
        if {path.rsplit("/", 1)[-1] for path in paths} != required_suffixes:
            raise ValueError("CToxPred2 RF-SSL identity must contain the four required artifacts")
        if any(not name.strip() or not version.strip() for name, version in self.packages.items()):
            raise ValueError("CToxPred2 package identity must not contain blank values")
        if self.packages != CTOX_PACKAGES:
            raise ValueError("CToxPred2 packages do not match the pinned RF-SSL runtime")
        if self.upstream_commit != CTOX_UPSTREAM_COMMIT:
            raise ValueError("CToxPred2 upstream commit is not the pinned revision")
        if {item.relative_path: item.sha256 for item in self.artifacts} != CTOX_ARTIFACTS:
            raise ValueError("CToxPred2 artifact hashes do not match the pinned RF-SSL profile")
        return self


class CtoxToolArguments(ContractModel):
    """고정 RF-SSL 추론의 최소 입력."""

    schema_version: Literal["1"] = "1"
    canonical_smiles: str = Field(min_length=1, max_length=5000)


class CtoxChannelPrediction(ContractModel):
    """한 채널의 분류 라벨과 해당 라벨의 RF predict_proba."""

    channel: CtoxChannel
    label: CtoxPredictionLabel
    class_probability: float = Field(ge=0, le=1, allow_inf_nan=False)


class CtoxToolResult(ContractModel):
    """세 채널을 누락 없이 보존하는 CToxPred2 결과."""

    schema_version: Literal["1"] = "1"
    tool_id: Literal["ctoxpred2"] = "ctoxpred2"
    canonical_smiles: str = Field(min_length=1, max_length=5000)
    model: CtoxModelIdentity
    predictions: tuple[CtoxChannelPrediction, ...] = Field(min_length=3, max_length=3)
    limitations: tuple[str, ...] = (
        "Predictions indicate ion-channel liability, not clinical QT prolongation or arrhythmia.",
        "Negative predictions do not establish cardiac safety.",
        "Cardiomyopathy, ventricular dysfunction, and heart failure are outside this model scope.",
    )

    @model_validator(mode="after")
    def validate_channels(self) -> Self:
        channels = [item.channel for item in self.predictions]
        if set(channels) != set(CtoxChannel) or len(channels) != len(set(channels)):
            raise ValueError("CToxPred2 result must contain each supported channel exactly once")
        if any(not item.strip() for item in self.limitations):
            raise ValueError("CToxPred2 limitations must not contain blank values")
        return self
