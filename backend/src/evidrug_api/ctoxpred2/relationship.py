"""ADMET-AI와 CToxPred2 관측의 비교 가능성을 보존하는 관계 계약."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from evidrug_api.admet.contracts import AdmetNormalizedOutput, Sha256
from evidrug_api.ctoxpred2.contracts import (
    CtoxChannel,
    CtoxPredictionLabel,
    CtoxToolResult,
)
from evidrug_api.execution_contracts.common import ContractModel


class ModelRelationshipStatus(StrEnum):
    """검증되지 않은 기준을 억지로 일치·상충으로 바꾸지 않는 상태."""

    RELATED_SIGNAL = "related_signal"
    UNRESOLVED = "unresolved"


class ChannelRelationship(ContractModel):
    ctox_channel: CtoxChannel
    baseline_endpoint_id: Literal["hERG"] | None = None
    status: ModelRelationshipStatus
    reason_code: Literal[
        "decision_criteria_unverified",
        "baseline_endpoint_unavailable",
        "additional_channel_without_baseline_counterpart",
    ]
    baseline_value: float | None = Field(default=None, allow_inf_nan=False)
    baseline_drugbank_percentile: float | None = Field(
        default=None, ge=0, le=100, allow_inf_nan=False
    )
    ctox_label: CtoxPredictionLabel
    ctox_class_probability: float = Field(ge=0, le=1, allow_inf_nan=False)
    interpretation: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def preserve_semantics(self) -> Self:
        has_baseline = self.baseline_endpoint_id is not None
        if has_baseline != (
            self.baseline_value is not None and self.baseline_drugbank_percentile is not None
        ):
            raise ValueError("baseline reference and values must be present together")
        if self.ctox_channel is CtoxChannel.HERG:
            if self.status is not ModelRelationshipStatus.UNRESOLVED:
                raise ValueError("hERG relationship must remain unresolved")
        elif (
            has_baseline
            or self.status is not ModelRelationshipStatus.RELATED_SIGNAL
            or self.reason_code != "additional_channel_without_baseline_counterpart"
        ):
            raise ValueError("additional channels must remain separate related signals")
        return self


class CtoxAdmetRelationshipReport(ContractModel):
    schema_version: Literal["1"] = "1"
    canonical_smiles: str = Field(min_length=1, max_length=5000)
    admet_manifest_sha256: Sha256
    ctox_tool_version: Literal["rf-ssl-2a31aa119e27-r1"] = "rf-ssl-2a31aa119e27-r1"
    relationships: tuple[ChannelRelationship, ...] = Field(min_length=3, max_length=3)
    limitations: tuple[str, ...] = (
        "ADMET-AI hERG and CToxPred2 hERG decision criteria are not verified as equivalent.",
        "ADMET percentile is a reference rank; CToxPred2 class_probability is the selected "
        "RF class probability.",
        "No score averaging, threshold translation, concordance, or discordance is inferred.",
    )

    @model_validator(mode="after")
    def exact_channels(self) -> Self:
        channels = [item.ctox_channel for item in self.relationships]
        if set(channels) != set(CtoxChannel) or len(channels) != len(set(channels)):
            raise ValueError("relationship report requires each CToxPred2 channel exactly once")
        return self


def relate_admet_and_ctox(
    admet: AdmetNormalizedOutput, ctox: CtoxToolResult
) -> CtoxAdmetRelationshipReport:
    """원 관측을 보존하며 비교 가능한 관계만 결정적으로 부여한다."""
    if admet.result.canonical_smiles != ctox.canonical_smiles:
        raise ValueError("relationship inputs must describe the same canonical SMILES")
    predictions = {item.endpoint_id: item for item in admet.result.predictions}
    definitions = {item.endpoint_id: item for item in admet.manifest.endpoints}
    ctox_predictions = {item.channel: item for item in ctox.predictions}
    herg = predictions.get("hERG")
    herg_definition = definitions.get("hERG")
    if (herg is None) != (herg_definition is None):
        raise ValueError("ADMET hERG definition and prediction must be present together")

    channel_rows: list[ChannelRelationship] = []
    for channel in CtoxChannel:
        prediction = ctox_predictions[channel]
        if channel is CtoxChannel.HERG and herg is not None:
            channel_rows.append(
                ChannelRelationship(
                    ctox_channel=channel,
                    baseline_endpoint_id="hERG",
                    status=ModelRelationshipStatus.UNRESOLVED,
                    reason_code="decision_criteria_unverified",
                    baseline_value=herg.value,
                    baseline_drugbank_percentile=herg.drugbank_approved_percentile,
                    ctox_label=prediction.label,
                    ctox_class_probability=prediction.class_probability,
                    interpretation=(
                        "같은 hERG 위험 영역의 모델 관측이지만 endpoint 정의와 판정 기준의 "
                        "동등성이 확인되지 않아 일치·상충을 판정하지 않습니다."
                    ),
                )
            )
        elif channel is CtoxChannel.HERG:
            channel_rows.append(
                ChannelRelationship(
                    ctox_channel=channel,
                    status=ModelRelationshipStatus.UNRESOLVED,
                    reason_code="baseline_endpoint_unavailable",
                    ctox_label=prediction.label,
                    ctox_class_probability=prediction.class_probability,
                    interpretation=(
                        "ADMET baseline에 hERG endpoint가 없어 관계를 해결하지 못했습니다."
                    ),
                )
            )
        else:
            channel_rows.append(
                ChannelRelationship(
                    ctox_channel=channel,
                    status=ModelRelationshipStatus.RELATED_SIGNAL,
                    reason_code="additional_channel_without_baseline_counterpart",
                    ctox_label=prediction.label,
                    ctox_class_probability=prediction.class_probability,
                    interpretation=(
                        "ADMET baseline의 직접 대응 endpoint가 없는 별도 심장 이온통로 신호로 "
                        "보존합니다."
                    ),
                )
            )
    return CtoxAdmetRelationshipReport(
        canonical_smiles=ctox.canonical_smiles,
        admet_manifest_sha256=admet.manifest.manifest_sha256,
        relationships=tuple(channel_rows),
    )
