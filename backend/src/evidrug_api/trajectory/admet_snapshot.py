"""ADMET 관측 snapshot 생성과 live fallback 없는 replay."""

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.admet.contracts import AdmetToolArguments, AdmetToolResult, Sha256
from evidrug_api.admet.repository import AdmetRepository
from evidrug_api.execution_contracts.common import (
    ArtifactReference,
    ComponentVersion,
    ContractModel,
    ExecutionError,
    ExecutionMetadata,
    ExecutionUsage,
)
from evidrug_api.execution_contracts.tool import (
    ToolAdmission,
    ToolAdmissionDecision,
    ToolObservation,
    ToolObservationStatus,
    ToolRequest,
)
from evidrug_api.tool_admission.tables import ToolAdmissionRecord
from evidrug_api.tool_execution.runner import input_fingerprint
from evidrug_api.tool_execution.tables import ToolExecutionRecord
from evidrug_api.trajectory.tables import AdmetObservationSnapshotRecord


class SnapshotSourceStatus(StrEnum):
    """Pydantic Literal과 SQL 문자열에 쓰는 source terminal 상태."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AdmetModelDataIdentity(ContractModel):
    """성공 관측을 만든 모델 bundle과 참조 데이터의 식별자."""

    manifest_sha256: Sha256
    endpoint_metadata_sha256: Sha256
    reference_sha256: Sha256
    model_artifact_sha256: tuple[Sha256, ...] = Field(min_length=1)


class AdmetSnapshotEntry(ContractModel):
    """한 원본 tool call의 불변 입력·출력·버전 fingerprint."""

    source_tool_call_id: UUID
    source_request_id: UUID
    source_run_id: UUID
    tool_id: Literal["admet_ai"] = "admet_ai"
    tool_version: str = Field(min_length=1, max_length=200)
    input_sha256: Sha256
    status: SnapshotSourceStatus
    error_code: str | None = Field(default=None, max_length=120)
    result_sha256: Sha256 | None = None
    model_data: AdmetModelDataIdentity | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == SnapshotSourceStatus.SUCCEEDED:
            if self.error_code is not None or self.result_sha256 is None or self.model_data is None:
                raise ValueError("successful snapshot entries require result and model identity")
        elif (
            self.error_code is None or self.result_sha256 is not None or self.model_data is not None
        ):
            raise ValueError("failed snapshot entries require only an error code")
        return self


class AdmetObservationSnapshot(ContractModel):
    """동일 관측 위에서 여러 선택 정책을 비교하는 versioned manifest."""

    schema_version: Literal["1"] = "1"
    snapshot_id: UUID
    snapshot_version: str = Field(min_length=1, max_length=200)
    analysis_id: UUID
    entries: tuple[AdmetSnapshotEntry, ...] = Field(min_length=1)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_unique_inputs(self) -> Self:
        keys = [(item.tool_id, item.tool_version, item.input_sha256) for item in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("snapshot entries must have unique tool/version/input keys")
        return self


class AdmetSnapshotError(RuntimeError):
    """snapshot 원본이 없거나 불변 조건과 일치하지 않는다."""


class AdmetSnapshotConflict(RuntimeError):
    """같은 snapshot 식별자가 다른 manifest에 사용됐다."""


def _result_sha256(result: AdmetToolResult) -> str:
    payload = result.model_dump_json()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AdmetSnapshotRepository:
    """원 tool ledger를 참조하는 snapshot manifest만 저장한다."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        analysis_id: UUID,
        snapshot_version: str,
        source_tool_call_ids: tuple[UUID, ...],
        snapshot_id: UUID | None = None,
    ) -> AdmetObservationSnapshot:
        if not source_tool_call_ids:
            raise AdmetSnapshotError("snapshot requires at least one source tool call")
        entries = tuple(
            [await self._entry(analysis_id, tool_call_id) for tool_call_id in source_tool_call_ids]
        )
        snapshot = AdmetObservationSnapshot(
            snapshot_id=snapshot_id or uuid4(),
            snapshot_version=snapshot_version,
            analysis_id=analysis_id,
            entries=entries,
            created_at=datetime.now(UTC),
        )
        payload = snapshot.model_dump_json()
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.session.add(
            AdmetObservationSnapshotRecord(
                snapshot_id=snapshot.snapshot_id,
                analysis_id=analysis_id,
                snapshot_version=snapshot_version,
                manifest_json=payload,
                manifest_sha256=digest,
                created_at=snapshot.created_at,
            )
        )
        try:
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            existing = await self.load(snapshot.snapshot_id)
            if existing == snapshot:
                return existing
            raise AdmetSnapshotConflict(
                "snapshot identifier already has different content"
            ) from error
        return snapshot

    async def load(self, snapshot_id: UUID) -> AdmetObservationSnapshot | None:
        row = await self.session.get(AdmetObservationSnapshotRecord, snapshot_id)
        if row is None:
            return None
        digest = hashlib.sha256(row.manifest_json.encode("utf-8")).hexdigest()
        if digest != row.manifest_sha256:
            raise AdmetSnapshotError("snapshot manifest hash mismatch")
        snapshot = AdmetObservationSnapshot.model_validate_json(row.manifest_json)
        if (
            snapshot.snapshot_id != row.snapshot_id
            or snapshot.analysis_id != row.analysis_id
            or snapshot.snapshot_version != row.snapshot_version
        ):
            raise AdmetSnapshotError("snapshot columns do not match manifest")
        return snapshot

    async def verify_sources(self, snapshot: AdmetObservationSnapshot) -> None:
        """저장 시점 이후 원 실행·결과·모델/데이터 식별자가 변하지 않았는지 확인한다."""
        for entry in snapshot.entries:
            if await self._entry(snapshot.analysis_id, entry.source_tool_call_id) != entry:
                raise AdmetSnapshotError("snapshot source changed")

    async def _entry(self, analysis_id: UUID, tool_call_id: UUID) -> AdmetSnapshotEntry:
        execution = await self.session.get(ToolExecutionRecord, tool_call_id)
        admission = await self.session.scalar(
            select(ToolAdmissionRecord).where(ToolAdmissionRecord.tool_call_id == tool_call_id)
        )
        if execution is None or admission is None:
            raise AdmetSnapshotError("source tool call or admission does not exist")
        if execution.analysis_id != analysis_id or execution.tool_id != "admet_ai":
            raise AdmetSnapshotError("source tool call is outside the ADMET analysis scope")
        if execution.status not in {SnapshotSourceStatus.SUCCEEDED, SnapshotSourceStatus.FAILED}:
            raise AdmetSnapshotError("source tool call is not terminal")
        if admission.decision != ToolAdmissionDecision.APPROVED.value:
            raise AdmetSnapshotError("source tool call was not approved")
        if execution.status == SnapshotSourceStatus.FAILED:
            if execution.error_code is None:
                raise AdmetSnapshotError("failed source tool call omitted its error code")
            return AdmetSnapshotEntry(
                source_tool_call_id=execution.tool_call_id,
                source_request_id=execution.request_id,
                source_run_id=execution.run_id,
                tool_version=admission.tool_version,
                input_sha256=execution.input_sha256,
                status=SnapshotSourceStatus.FAILED,
                error_code=execution.error_code,
            )
        output = await AdmetRepository(self.session).load_output(tool_call_id)
        if output is None:
            raise AdmetSnapshotError("successful source tool call omitted its ADMET result")
        return AdmetSnapshotEntry(
            source_tool_call_id=execution.tool_call_id,
            source_request_id=execution.request_id,
            source_run_id=execution.run_id,
            tool_version=admission.tool_version,
            input_sha256=execution.input_sha256,
            status=SnapshotSourceStatus.SUCCEEDED,
            result_sha256=_result_sha256(output.result),
            model_data=AdmetModelDataIdentity(
                manifest_sha256=output.manifest.manifest_sha256,
                endpoint_metadata_sha256=output.manifest.endpoint_metadata_sha256,
                reference_sha256=output.manifest.reference_sha256,
                model_artifact_sha256=tuple(
                    item.sha256 for item in output.manifest.model_artifacts
                ),
            ),
        )


class AdmetReplayExecutor:
    """snapshot miss를 live 호출로 숨기지 않는 ADMET typed observation executor."""

    POLICY_VERSION = "admet-snapshot-replay-v1"

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.snapshots = AdmetSnapshotRepository(session)

    async def execute(
        self,
        snapshot_id: UUID,
        *,
        tool_call_id: UUID,
        request: ToolRequest[AdmetToolArguments],
    ) -> ToolObservation[AdmetToolResult]:
        started_at = datetime.now(UTC)
        started = perf_counter()
        snapshot = await self.snapshots.load(snapshot_id)
        if snapshot is None:
            return self._failed(
                tool_call_id, request, "replay_snapshot_missing", started_at, started
            )
        if request.tool_id != "admet_ai":
            return self._failed(tool_call_id, request, "replay_tool_missing", started_at, started)
        versions = {entry.tool_version for entry in snapshot.entries}
        if request.tool_version not in versions:
            return self._failed(
                tool_call_id, request, "replay_tool_version_mismatch", started_at, started
            )
        fingerprint = input_fingerprint(
            {
                "arguments": request.arguments.model_dump(mode="json"),
                "tool_version": request.tool_version,
            }
        )
        entry = next(
            (
                item
                for item in snapshot.entries
                if item.tool_version == request.tool_version and item.input_sha256 == fingerprint
            ),
            None,
        )
        if entry is None:
            return self._failed(tool_call_id, request, "replay_input_mismatch", started_at, started)
        if entry.status == SnapshotSourceStatus.FAILED:
            return self._failed(
                tool_call_id,
                request,
                entry.error_code or "replay_source_failed",
                started_at,
                started,
                snapshot=snapshot,
            )
        output = await AdmetRepository(self.session).load_output(entry.source_tool_call_id)
        if output is None or _result_sha256(output.result) != entry.result_sha256:
            return self._failed(tool_call_id, request, "replay_source_changed", started_at, started)
        if entry.model_data is None or (
            output.manifest.manifest_sha256 != entry.model_data.manifest_sha256
            or output.manifest.endpoint_metadata_sha256 != entry.model_data.endpoint_metadata_sha256
            or output.manifest.reference_sha256 != entry.model_data.reference_sha256
            or tuple(item.sha256 for item in output.manifest.model_artifacts)
            != entry.model_data.model_artifact_sha256
        ):
            return self._failed(
                tool_call_id, request, "replay_source_version_mismatch", started_at, started
            )
        return ToolObservation[AdmetToolResult](
            tool_call_id=tool_call_id,
            request_id=request.request_id,
            run_id=request.run_id,
            status=ToolObservationStatus.SUCCEEDED,
            admission=self._admission(),
            result=output.result,
            raw_result=ArtifactReference(
                artifact_id=entry.source_tool_call_id,
                schema_name="admet_tool_result",
                schema_version=output.result.schema_version,
                media_type="application/json",
                sha256=entry.result_sha256,
            ),
            execution_metadata=self._metadata(
                request, started_at, started, snapshot=snapshot, source=entry
            ),
        )

    def _failed(
        self,
        tool_call_id: UUID,
        request: ToolRequest[AdmetToolArguments],
        code: str,
        started_at: datetime,
        started: float,
        *,
        snapshot: AdmetObservationSnapshot | None = None,
    ) -> ToolObservation[AdmetToolResult]:
        return ToolObservation[AdmetToolResult](
            tool_call_id=tool_call_id,
            request_id=request.request_id,
            run_id=request.run_id,
            status=ToolObservationStatus.FAILED,
            admission=self._admission(),
            error=ExecutionError(
                code=code,
                message="저장된 ADMET 관측을 replay하지 못했습니다.",
                retryable=False,
            ),
            execution_metadata=self._metadata(request, started_at, started, snapshot=snapshot),
        )

    def _admission(self) -> ToolAdmission:
        return ToolAdmission(
            decision=ToolAdmissionDecision.APPROVED,
            policy_version=self.POLICY_VERSION,
        )

    def _metadata(
        self,
        request: ToolRequest[AdmetToolArguments],
        started_at: datetime,
        started: float,
        *,
        snapshot: AdmetObservationSnapshot | None,
        source: AdmetSnapshotEntry | None = None,
    ) -> ExecutionMetadata:
        components = [
            ComponentVersion(component="tool:" + request.tool_id, version=request.tool_version),
            ComponentVersion(component="replay_policy", version=self.POLICY_VERSION),
        ]
        if snapshot is not None:
            components.append(
                ComponentVersion(
                    component="observation_snapshot", version=snapshot.snapshot_version
                )
            )
        if source is not None and source.model_data is not None:
            components.append(
                ComponentVersion(
                    component="admet_manifest", version=source.model_data.manifest_sha256
                )
            )
        return ExecutionMetadata(
            started_at=started_at,
            finished_at=datetime.now(UTC),
            duration_ms=int((perf_counter() - started) * 1000),
            implementation_version="admet-observation-replay-v1",
            components=tuple(components),
            usage=ExecutionUsage(),
        )
