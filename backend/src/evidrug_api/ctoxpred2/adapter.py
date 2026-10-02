"""격리 runtime report를 CToxPred2 typed result로 변환한다."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evidrug_api.ctoxpred2.contracts import (
    CtoxChannel,
    CtoxChannelPrediction,
    CtoxModelArtifact,
    CtoxModelIdentity,
    CtoxPredictionLabel,
    CtoxToolArguments,
    CtoxToolResult,
    GitCommit,
    Sha256,
)


class CtoxReportError(ValueError):
    """runtime report가 고정 RF-SSL 계약을 위반한 경우."""


class _RawChannelPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: Literal[0, 1]
    class_probability: float = Field(ge=0, le=1, allow_inf_nan=False)


class _RawReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed"]
    canonical_smiles: str = Field(min_length=1, max_length=5000)
    upstream_repository: Literal["issararab/CToxPred2"]
    upstream_commit: GitCommit
    profile: Literal["rf_ssl"]
    packages: dict[str, str]
    artifacts: dict[str, Sha256]
    predictions: dict[str, _RawChannelPrediction]

    @field_validator("packages", "artifacts", mode="before")
    @classmethod
    def reject_non_string_mappings(cls, value: object) -> dict[str, str]:
        if not isinstance(value, Mapping) or not value:
            raise ValueError("identity mappings must not be empty")
        normalized: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise ValueError("identity mappings must contain strings")
            normalized[key] = item
        return normalized


class CtoxReportProvider(Protocol):
    """격리 subprocess와 test double이 구현할 report provider."""

    async def predict(self, canonical_smiles: str) -> Mapping[str, object]:
        """한 분자의 RF-SSL report를 반환한다."""


@dataclass(frozen=True, slots=True)
class CtoxToolAdapter:
    """provider 실행과 순수 report 정규화를 결합한다."""

    provider: CtoxReportProvider

    async def execute(self, arguments: CtoxToolArguments) -> CtoxToolResult:
        report = await self.provider.predict(arguments.canonical_smiles)
        return normalize_ctox_report(report, expected_smiles=arguments.canonical_smiles)


def normalize_ctox_report(
    raw_report: Mapping[str, object], *, expected_smiles: str | None = None
) -> CtoxToolResult:
    """upstream의 0/1과 max predict_proba를 오해 없이 typed 결과로 만든다."""

    try:
        report = _RawReport.model_validate(raw_report)
    except ValueError as error:
        raise CtoxReportError("invalid CToxPred2 provider report") from error
    if expected_smiles is not None and report.canonical_smiles != expected_smiles:
        raise CtoxReportError("provider returned predictions for a different SMILES")

    channel_names = {
        "hERG": CtoxChannel.HERG,
        "Nav1.5": CtoxChannel.NAV1_5,
        "Cav1.2": CtoxChannel.CAV1_2,
    }
    if set(report.predictions) != set(channel_names):
        raise CtoxReportError("provider report must contain exactly three channel predictions")
    try:
        model = CtoxModelIdentity(
            upstream_repository=report.upstream_repository,
            upstream_commit=report.upstream_commit,
            profile=report.profile,
            packages=report.packages,
            artifacts=tuple(
                CtoxModelArtifact(relative_path=path, sha256=sha256)
                for path, sha256 in sorted(report.artifacts.items())
            ),
        )
        predictions = tuple(
            CtoxChannelPrediction(
                channel=channel,
                label=(
                    CtoxPredictionLabel.POSITIVE
                    if report.predictions[name].label == 1
                    else CtoxPredictionLabel.NEGATIVE
                ),
                class_probability=report.predictions[name].class_probability,
            )
            for name, channel in channel_names.items()
        )
        return CtoxToolResult(
            canonical_smiles=report.canonical_smiles,
            model=model,
            predictions=predictions,
        )
    except ValueError as error:
        raise CtoxReportError("CToxPred2 report violates the normalized contract") from error
