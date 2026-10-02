"""ADMET-AI 1.4 report를 stable manifest와 prediction 행으로 변환한다."""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Real
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evidrug_api.admet.contracts import (
    AdmetEndpointDefinition,
    AdmetEndpointPrediction,
    AdmetModelArtifact,
    AdmetModelManifest,
    AdmetNormalizedOutput,
    AdmetTaskType,
    AdmetToolArguments,
    AdmetToolResult,
)

PERCENTILE_SUFFIX = "_drugbank_approved_percentile"
_EMPTY_METADATA_VALUES = {"", "-"}
_POSITIVE_INFINITY_VALUES = {"inf", "+inf", "infinity", "+infinity"}
_NEGATIVE_INFINITY_VALUES = {"-inf", "-infinity"}


class AdmetReportError(ValueError):
    """ADMET provider report가 정규화 계약을 충족하지 못했을 때 발생한다."""


class _RawEndpointMetadata(BaseModel):
    """ADMET-AI에 동봉된 CSV 한 행의 고정 v1.4 shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str
    id: str
    name: str
    size: str
    task_type: AdmetTaskType
    units: str
    minimum: str
    maximum: str
    species: str
    tdc_rank: str
    auprc: str = Field(alias="AUPRC")
    auroc: str = Field(alias="AUROC")
    r_squared: str = Field(alias="R^2")
    mae: str = Field(alias="MAE")
    url: str


class _ProviderReport(BaseModel):
    """smoke와 production provider가 공통으로 제공해야 하는 report subset."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    status: Literal["passed"]
    smiles: str = Field(min_length=1)
    packages: dict[str, str]
    model_sha256: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]]
    reference_population: str = Field(min_length=1)
    reference_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    endpoint_metadata_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    endpoint_metadata: tuple[_RawEndpointMetadata, ...] = Field(min_length=1)
    predictions: dict[str, float]
    limitations: tuple[str, ...] = ()

    @field_validator("predictions", mode="before")
    @classmethod
    def reject_invalid_predictions(cls, value: object) -> dict[str, float]:
        """bool, 비수치와 비유한 값을 Pydantic coercion 전에 거부한다."""

        if not isinstance(value, Mapping) or not value:
            raise ValueError("predictions must be a non-empty mapping")
        predictions: dict[str, float] = {}
        for endpoint_id, prediction in value.items():
            if not isinstance(endpoint_id, str) or not endpoint_id:
                raise ValueError("prediction keys must be non-empty strings")
            if isinstance(prediction, bool) or not isinstance(prediction, Real):
                raise ValueError("prediction values must be numeric")
            normalized = float(prediction)
            if not math.isfinite(normalized):
                raise ValueError("prediction values must be finite")
            predictions[endpoint_id] = normalized
        return predictions


class AdmetReportProvider(Protocol):
    """Docker service나 test double이 구현할 raw report provider."""

    async def predict(self, canonical_smiles: str) -> Mapping[str, object]:
        """한 분자의 ADMET-AI report를 반환한다."""


@dataclass(frozen=True, slots=True)
class AdmetToolAdapter:
    """provider 실행과 순수 정규화 로직을 결합한 공통 tool surface."""

    provider: AdmetReportProvider

    async def execute(self, arguments: AdmetToolArguments) -> AdmetNormalizedOutput:
        """고정 계획과 Agents SDK function tool이 함께 호출할 실행 경계."""

        report = await self.provider.predict(arguments.canonical_smiles)
        return normalize_admet_report(report, expected_smiles=arguments.canonical_smiles)


def normalize_admet_report(
    raw_report: Mapping[str, object],
    *,
    expected_smiles: str | None = None,
) -> AdmetNormalizedOutput:
    """ADMET-AI report를 catalog 중복이 없는 내부 계약으로 변환한다."""

    try:
        report = _ProviderReport.model_validate(raw_report)
    except ValueError as error:
        raise AdmetReportError("invalid ADMET provider report") from error

    try:
        return _normalize_validated_report(report, expected_smiles=expected_smiles)
    except AdmetReportError:
        raise
    except ValueError as error:
        raise AdmetReportError("ADMET report violates the normalized contract") from error


def _normalize_validated_report(
    report: _ProviderReport,
    *,
    expected_smiles: str | None,
) -> AdmetNormalizedOutput:
    """검증된 provider shape를 공개 계약으로 조립한다."""

    if expected_smiles is not None and report.smiles != expected_smiles:
        raise AdmetReportError("provider returned predictions for a different SMILES")
    tool_version = report.packages.get("admet-ai")
    if not tool_version:
        raise AdmetReportError("provider report omitted the admet-ai package version")

    endpoints = tuple(_normalize_endpoint(row) for row in report.endpoint_metadata)
    endpoint_by_id = {endpoint.endpoint_id: endpoint for endpoint in endpoints}
    if len(endpoint_by_id) != len(endpoints):
        raise AdmetReportError("endpoint metadata contains duplicate ids")

    expected_prediction_keys = set(endpoint_by_id)
    expected_prediction_keys.update(
        f"{endpoint_id}{PERCENTILE_SUFFIX}" for endpoint_id in endpoint_by_id
    )
    actual_prediction_keys = set(report.predictions)
    if actual_prediction_keys != expected_prediction_keys:
        missing = sorted(expected_prediction_keys - actual_prediction_keys)
        unexpected = sorted(actual_prediction_keys - expected_prediction_keys)
        raise AdmetReportError(
            f"prediction keys do not match endpoint metadata: missing={missing}, "
            f"unexpected={unexpected}"
        )

    predictions = []
    for endpoint in endpoints:
        value = report.predictions[endpoint.endpoint_id]
        percentile = report.predictions[f"{endpoint.endpoint_id}{PERCENTILE_SUFFIX}"]
        if endpoint.task_type is AdmetTaskType.CLASSIFICATION and not 0 <= value <= 1:
            raise AdmetReportError(
                f"classification prediction must be between 0 and 1: {endpoint.endpoint_id}"
            )
        if not 0 <= percentile <= 100:
            raise AdmetReportError(
                f"DrugBank percentile must be between 0 and 100: {endpoint.endpoint_id}"
            )
        predictions.append(
            AdmetEndpointPrediction(
                endpoint_id=endpoint.endpoint_id,
                value=value,
                drugbank_approved_percentile=percentile,
            )
        )

    model_artifacts = tuple(
        AdmetModelArtifact(relative_path=path, sha256=digest)
        for path, digest in sorted(report.model_sha256.items())
    )
    if not model_artifacts:
        raise AdmetReportError("provider report omitted model artifact hashes")
    limitations = tuple(limitation.strip() for limitation in report.limitations)
    manifest_sha256 = _manifest_sha256(
        tool_version=tool_version,
        endpoint_metadata_sha256=report.endpoint_metadata_sha256,
        reference_population=report.reference_population,
        reference_sha256=report.reference_sha256,
        model_artifacts=model_artifacts,
        endpoints=endpoints,
        limitations=limitations,
    )
    manifest = AdmetModelManifest(
        manifest_sha256=manifest_sha256,
        tool_version=tool_version,
        endpoint_metadata_sha256=report.endpoint_metadata_sha256,
        reference_population=report.reference_population,
        reference_sha256=report.reference_sha256,
        model_artifacts=model_artifacts,
        endpoints=endpoints,
        limitations=limitations,
    )
    result = AdmetToolResult(
        manifest_sha256=manifest_sha256,
        canonical_smiles=report.smiles,
        predictions=tuple(predictions),
    )
    return AdmetNormalizedOutput(manifest=manifest, result=result)


def _normalize_endpoint(raw: _RawEndpointMetadata) -> AdmetEndpointDefinition:
    minimum, minimum_unbounded = _parse_bound(raw.minimum, lower=True)
    maximum, maximum_unbounded = _parse_bound(raw.maximum, lower=False)
    return AdmetEndpointDefinition(
        endpoint_id=_required_text(raw.id, "id"),
        category=_required_text(raw.category, "category"),
        name=_required_text(raw.name, "name"),
        task_type=raw.task_type,
        dataset_size=_optional_integer(raw.size, "size", minimum=0),
        units=_optional_text(raw.units),
        minimum=minimum,
        maximum=maximum,
        minimum_unbounded=minimum_unbounded,
        maximum_unbounded=maximum_unbounded,
        species=_optional_text(raw.species),
        tdc_rank=_optional_integer(raw.tdc_rank, "tdc_rank", minimum=1),
        auprc=_optional_float(raw.auprc, "AUPRC"),
        auroc=_optional_float(raw.auroc, "AUROC"),
        r_squared=_optional_float(raw.r_squared, "R^2"),
        mae=_optional_float(raw.mae, "MAE"),
        source_url=_optional_text(raw.url),
    )


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if normalized in _EMPTY_METADATA_VALUES:
        raise AdmetReportError(f"endpoint metadata requires {field_name}")
    return normalized


def _optional_text(value: str) -> str | None:
    normalized = value.strip()
    return None if normalized in _EMPTY_METADATA_VALUES else normalized


def _optional_integer(value: str, field_name: str, *, minimum: int) -> int | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    try:
        parsed = int(normalized)
    except ValueError as error:
        raise AdmetReportError(f"endpoint metadata {field_name} must be an integer") from error
    if parsed < minimum:
        raise AdmetReportError(f"endpoint metadata {field_name} is below its minimum")
    return parsed


def _optional_float(value: str, field_name: str) -> float | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    try:
        parsed = float(normalized)
    except ValueError as error:
        raise AdmetReportError(f"endpoint metadata {field_name} must be numeric") from error
    if not math.isfinite(parsed):
        raise AdmetReportError(f"endpoint metadata {field_name} must be finite")
    return parsed


def _parse_bound(value: str, *, lower: bool) -> tuple[float | None, bool]:
    normalized = value.strip().lower()
    if normalized in _EMPTY_METADATA_VALUES:
        return None, False
    if normalized in _NEGATIVE_INFINITY_VALUES:
        if not lower:
            raise AdmetReportError("maximum must not be negative infinity")
        return None, True
    if normalized in _POSITIVE_INFINITY_VALUES:
        if lower:
            raise AdmetReportError("minimum must not be positive infinity")
        return None, True
    parsed = _optional_float(value, "bound")
    if parsed is None:
        raise AdmetReportError("endpoint bound must not be blank")
    return parsed, False


def _manifest_sha256(
    *,
    tool_version: str,
    endpoint_metadata_sha256: str,
    reference_population: str,
    reference_sha256: str,
    model_artifacts: tuple[AdmetModelArtifact, ...],
    endpoints: tuple[AdmetEndpointDefinition, ...],
    limitations: tuple[str, ...],
) -> str:
    payload = {
        "schema_version": "1",
        "tool_id": "admet_ai",
        "tool_version": tool_version,
        "endpoint_metadata_sha256": endpoint_metadata_sha256,
        "reference_population": reference_population,
        "reference_sha256": reference_sha256,
        "model_artifacts": [artifact.model_dump(mode="json") for artifact in model_artifacts],
        "endpoints": [endpoint.model_dump(mode="json") for endpoint in endpoints],
        "limitations": limitations,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
