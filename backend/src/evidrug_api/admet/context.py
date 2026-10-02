"""저장된 ADMET 결과를 분석 범위 안에서 선택해 결정적으로 축약한다."""

from typing import Annotated, Literal, NamedTuple, Self
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.admet.contracts import AdmetTaskType, Sha256
from evidrug_api.admet.tables import (
    AdmetEndpointRecord,
    AdmetManifestLimitationRecord,
    AdmetModelManifestRecord,
    AdmetPredictionRecord,
    AdmetToolExecutionRecord,
)
from evidrug_api.execution_contracts.common import ContractModel

MAX_CONTEXT_ENDPOINTS = 20
MAX_CONTEXT_BYTES = 32_768
EndpointId = Annotated[str, Field(min_length=1, max_length=160, pattern=r"\S")]
FiniteValue = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Percentile = Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)]


class AdmetContextQuery(ContractModel):
    """호출자는 별도로 인증된 분석 범위를 전달한다. 전체 endpoint 암묵 조회는 없다."""

    source_tool_call_id: UUID
    endpoint_ids: tuple[EndpointId, ...] = Field(min_length=1, max_length=MAX_CONTEXT_ENDPOINTS)

    @model_validator(mode="after")
    def unique_endpoints(self) -> Self:
        if len(set(self.endpoint_ids)) != len(self.endpoint_ids):
            raise ValueError("duplicate endpoint selection")
        return self


class AdmetContextRow(NamedTuple):
    """JSON에서는 배열로 직렬화한다. 열 이름은 context에 한 번만 제공한다."""

    endpoint_id: str
    name: str
    category: str
    task_type: AdmetTaskType
    value: FiniteValue
    units: str | None
    drugbank_approved_percentile: Percentile
    species: str | None
    source_url: str | None


class AdmetContext(ContractModel):
    """과학적 판정이 아닌 선택 projection. 원본 참조와 해석 제한을 보존한다."""

    projection_version: Literal["admet-context-v1"] = "admet-context-v1"
    source_tool_call_id: UUID
    source_run_id: UUID
    manifest_sha256: Sha256
    tool_id: Literal["admet_ai"] = "admet_ai"
    tool_version: str
    endpoint_metadata_sha256: Sha256
    reference_population: str
    reference_sha256: Sha256
    catalog_endpoint_count: int = Field(ge=1)
    limitations: tuple[str, ...]
    interpretation_note: Literal[
        "Predictions are not experimental measurements. Percentile is reference rank, "
        "not confidence or a universal safety direction. Null units mean unspecified units."
    ] = (
        "Predictions are not experimental measurements. Percentile is reference rank, "
        "not confidence or a universal safety direction. Null units mean unspecified units."
    )
    rows: tuple[AdmetContextRow, ...] = Field(min_length=1, max_length=MAX_CONTEXT_ENDPOINTS)
    columns: tuple[str, ...] = AdmetContextRow._fields
    is_partial: bool

    @model_validator(mode="after")
    def validate_rows(self) -> Self:
        if self.columns != AdmetContextRow._fields:
            raise ValueError("invalid context columns")
        if self.is_partial != (len(self.rows) < self.catalog_endpoint_count):
            raise ValueError("invalid partial selection flag")
        if len({row.endpoint_id for row in self.rows}) != len(self.rows):
            raise ValueError("duplicate context endpoint")
        if len(self.rows) > self.catalog_endpoint_count:
            raise ValueError("invalid endpoint count")
        if any(
            row.task_type is AdmetTaskType.CLASSIFICATION and not 0 <= row.value <= 1
            for row in self.rows
        ):
            raise ValueError("classification prediction outside probability range")
        return self


class AdmetContextUnavailable(RuntimeError):
    """조회 실패의 고정 코드. 원본 입력·타 분석 존재 여부를 공개하지 않는다."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AdmetContextBuilder:
    """SELECT만 수행한다. provider, ledger, commit, 자동 fallback을 호출하지 않는다."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def build(self, *, analysis_id: UUID, query: AdmetContextQuery) -> AdmetContext:
        """인증된 analysis ID 안의 정확한 실행을 조회한다. 일부 누락도 실패 처리한다."""
        # ORM autoflush가 호출자의 pending 변경을 쓰지 않도록 읽기 경계를 고정한다.
        with self.session.no_autoflush:
            context = await self._read(analysis_id, query)
        if len(context.model_dump_json().encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise AdmetContextUnavailable("context_too_large")
        return context

    async def _read(self, analysis_id: UUID, query: AdmetContextQuery) -> AdmetContext:
        header = (
            await self.session.execute(
                select(
                    AdmetToolExecutionRecord.run_id,
                    AdmetModelManifestRecord.id,
                    AdmetModelManifestRecord.schema_version,
                    AdmetModelManifestRecord.tool_id,
                    AdmetModelManifestRecord.manifest_sha256,
                    AdmetModelManifestRecord.tool_version,
                    AdmetModelManifestRecord.endpoint_metadata_sha256,
                    AdmetModelManifestRecord.reference_population,
                    AdmetModelManifestRecord.reference_sha256,
                )
                .select_from(AdmetToolExecutionRecord)
                .join(
                    AdmetModelManifestRecord,
                    AdmetToolExecutionRecord.manifest_id == AdmetModelManifestRecord.id,
                )
                .where(
                    AdmetToolExecutionRecord.analysis_id == analysis_id,
                    AdmetToolExecutionRecord.tool_call_id == query.source_tool_call_id,
                )
            )
        ).one_or_none()
        if header is None:
            raise AdmetContextUnavailable("result_not_found")
        if header.schema_version != "1" or header.tool_id != "admet_ai":
            raise AdmetContextUnavailable("unsupported_manifest")
        selected = (
            await self.session.execute(
                select(
                    AdmetEndpointRecord.endpoint_id,
                    AdmetEndpointRecord.name,
                    AdmetEndpointRecord.category,
                    AdmetEndpointRecord.task_type,
                    AdmetPredictionRecord.value,
                    AdmetEndpointRecord.units,
                    AdmetPredictionRecord.drugbank_approved_percentile,
                    AdmetEndpointRecord.species,
                    AdmetEndpointRecord.source_url,
                )
                .select_from(AdmetEndpointRecord)
                .join(
                    AdmetPredictionRecord,
                    AdmetPredictionRecord.endpoint_record_id == AdmetEndpointRecord.id,
                )
                .where(
                    AdmetPredictionRecord.tool_call_id == query.source_tool_call_id,
                    AdmetPredictionRecord.manifest_id == header.id,
                    AdmetEndpointRecord.manifest_id == header.id,
                    AdmetEndpointRecord.endpoint_id.in_(query.endpoint_ids),
                )
            )
        ).all()
        rows = {row.endpoint_id: AdmetContextRow(*row) for row in selected}
        if set(rows) != set(query.endpoint_ids):
            raise AdmetContextUnavailable("endpoint_unavailable")
        count = await self.session.scalar(
            select(func.count())
            .select_from(AdmetEndpointRecord)
            .where(
                AdmetEndpointRecord.manifest_id == header.id,
            )
        )
        limitations = await self.session.scalars(
            select(AdmetManifestLimitationRecord.limitation)
            .where(
                AdmetManifestLimitationRecord.manifest_id == header.id,
            )
            .order_by(AdmetManifestLimitationRecord.position)
        )
        return AdmetContext(
            source_tool_call_id=query.source_tool_call_id,
            source_run_id=header.run_id,
            manifest_sha256=header.manifest_sha256,
            tool_version=header.tool_version,
            endpoint_metadata_sha256=header.endpoint_metadata_sha256,
            reference_population=header.reference_population,
            reference_sha256=header.reference_sha256,
            catalog_endpoint_count=count or 0,
            is_partial=len(rows) < (count or 0),
            limitations=tuple(limitations),
            rows=tuple(rows[key] for key in query.endpoint_ids),
        )
