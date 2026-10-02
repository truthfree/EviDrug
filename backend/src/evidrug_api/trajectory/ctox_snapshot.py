"""CToxPred2 관측 snapshot 생성과 live fallback 없는 replay."""

import hashlib
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.ctoxpred2.contracts import (
    CTOX_TOOL_VERSION,
    CtoxModelIdentity,
    CtoxToolArguments,
    CtoxToolResult,
    Sha256,
)
from evidrug_api.ctoxpred2.repository import CtoxRepository
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
from evidrug_api.trajectory.tables import CtoxObservationSnapshotRecord


class CtoxSnapshotEntry(ContractModel):
    source_tool_call_id: UUID
    source_request_id: UUID
    source_run_id: UUID
    tool_id: Literal["ctoxpred2"] = "ctoxpred2"
    tool_version: str = Field(min_length=1, max_length=200)
    input_sha256: Sha256
    status: Literal["succeeded", "failed"]
    error_code: str | None = Field(default=None, max_length=120)
    result_sha256: Sha256 | None = None
    model: CtoxModelIdentity | None = None

    @model_validator(mode="after")
    def valid_outcome(self) -> Self:
        if self.status == "succeeded":
            if self.error_code is not None or self.result_sha256 is None or self.model is None:
                raise ValueError("successful entry requires result and model identity")
        elif self.error_code is None or self.result_sha256 is not None or self.model is not None:
            raise ValueError("failed entry requires only an error code")
        return self


class CtoxObservationSnapshot(ContractModel):
    schema_version: Literal["1"] = "1"
    snapshot_id: UUID
    snapshot_version: str = Field(min_length=1, max_length=200)
    analysis_id: UUID
    entries: tuple[CtoxSnapshotEntry, ...] = Field(min_length=1)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def unique_inputs(self) -> Self:
        keys = [(item.tool_version, item.input_sha256) for item in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("snapshot entries must have unique version/input keys")
        return self


class CtoxSnapshotError(RuntimeError):
    pass


class CtoxSnapshotConflict(RuntimeError):
    pass


def _result_sha256(result: CtoxToolResult) -> str:
    return hashlib.sha256(result.model_dump_json().encode()).hexdigest()


class CtoxSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        analysis_id: UUID,
        snapshot_version: str,
        source_tool_call_ids: tuple[UUID, ...],
        snapshot_id: UUID | None = None,
    ) -> CtoxObservationSnapshot:
        if not source_tool_call_ids:
            raise CtoxSnapshotError("snapshot requires a source tool call")
        snapshot = CtoxObservationSnapshot(
            snapshot_id=snapshot_id or uuid4(),
            snapshot_version=snapshot_version,
            analysis_id=analysis_id,
            entries=tuple([await self._entry(analysis_id, item) for item in source_tool_call_ids]),
            created_at=datetime.now(UTC),
        )
        payload = snapshot.model_dump_json()
        self.session.add(
            CtoxObservationSnapshotRecord(
                snapshot_id=snapshot.snapshot_id,
                analysis_id=analysis_id,
                snapshot_version=snapshot_version,
                manifest_json=payload,
                manifest_sha256=hashlib.sha256(payload.encode()).hexdigest(),
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
            raise CtoxSnapshotConflict("snapshot identity has different content") from error
        return snapshot

    async def load(self, snapshot_id: UUID) -> CtoxObservationSnapshot | None:
        row = await self.session.get(CtoxObservationSnapshotRecord, snapshot_id)
        if row is None:
            return None
        if hashlib.sha256(row.manifest_json.encode()).hexdigest() != row.manifest_sha256:
            raise CtoxSnapshotError("snapshot manifest hash mismatch")
        snapshot = CtoxObservationSnapshot.model_validate_json(row.manifest_json)
        if (
            snapshot.snapshot_id != row.snapshot_id
            or snapshot.analysis_id != row.analysis_id
            or snapshot.snapshot_version != row.snapshot_version
        ):
            raise CtoxSnapshotError("snapshot columns do not match manifest")
        return snapshot

    async def verify_sources(self, snapshot: CtoxObservationSnapshot) -> None:
        """원 실행과 결과의 hash·모델 identity를 다시 대조한다."""
        for entry in snapshot.entries:
            if await self._entry(snapshot.analysis_id, entry.source_tool_call_id) != entry:
                raise CtoxSnapshotError("snapshot source changed")

    async def _entry(self, analysis_id: UUID, tool_call_id: UUID) -> CtoxSnapshotEntry:
        execution = await self.session.get(ToolExecutionRecord, tool_call_id)
        admission = await self.session.scalar(
            select(ToolAdmissionRecord).where(ToolAdmissionRecord.tool_call_id == tool_call_id)
        )
        if execution is None or admission is None:
            raise CtoxSnapshotError("source execution or admission is missing")
        if execution.analysis_id != analysis_id or execution.tool_id != "ctoxpred2":
            raise CtoxSnapshotError("source is outside the CToxPred2 analysis scope")
        if admission.decision != ToolAdmissionDecision.APPROVED.value:
            raise CtoxSnapshotError("source was not approved")
        if execution.status == "failed":
            if execution.error_code is None:
                raise CtoxSnapshotError("failed source omitted error code")
            return CtoxSnapshotEntry(
                source_tool_call_id=tool_call_id,
                source_request_id=execution.request_id,
                source_run_id=execution.run_id,
                tool_version=admission.tool_version,
                input_sha256=execution.input_sha256,
                status="failed",
                error_code=execution.error_code,
            )
        if execution.status != "succeeded":
            raise CtoxSnapshotError("source is not terminal")
        result = await CtoxRepository(self.session).load(tool_call_id)
        if result is None:
            raise CtoxSnapshotError("successful source omitted result")
        return CtoxSnapshotEntry(
            source_tool_call_id=tool_call_id,
            source_request_id=execution.request_id,
            source_run_id=execution.run_id,
            tool_version=admission.tool_version,
            input_sha256=execution.input_sha256,
            status="succeeded",
            result_sha256=_result_sha256(result),
            model=result.model,
        )


class CtoxReplayExecutor:
    POLICY_VERSION = "ctoxpred2-snapshot-replay-v1"

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.snapshots = CtoxSnapshotRepository(session)

    async def execute(
        self,
        snapshot_id: UUID,
        *,
        tool_call_id: UUID,
        request: ToolRequest[CtoxToolArguments],
        expected_analysis_id: UUID,
    ) -> ToolObservation[CtoxToolResult]:
        started_at, started = datetime.now(UTC), perf_counter()
        try:
            snapshot = await self.snapshots.load(snapshot_id)
        except CtoxSnapshotError:
            return self._failed(
                tool_call_id, request, "replay_snapshot_invalid", started_at, started
            )
        if snapshot is None:
            return self._failed(
                tool_call_id, request, "replay_snapshot_missing", started_at, started
            )
        if snapshot.analysis_id != expected_analysis_id:
            return self._failed(
                tool_call_id, request, "replay_analysis_mismatch", started_at, started
            )
        if request.tool_id != "ctoxpred2":
            return self._failed(tool_call_id, request, "replay_tool_missing", started_at, started)
        if request.tool_version != CTOX_TOOL_VERSION:
            return self._failed(
                tool_call_id, request, "replay_tool_version_mismatch", started_at, started
            )
        fingerprint = input_fingerprint(
            {
                "arguments": request.arguments.model_dump(mode="json"),
                "tool_version": request.tool_version,
            }
        )
        entry = next((item for item in snapshot.entries if item.input_sha256 == fingerprint), None)
        if entry is None:
            return self._failed(tool_call_id, request, "replay_input_mismatch", started_at, started)
        if entry.status == "failed":
            return self._failed(
                tool_call_id,
                request,
                entry.error_code or "replay_source_failed",
                started_at,
                started,
            )
        result = await CtoxRepository(self.session).load(entry.source_tool_call_id)
        if (
            result is None
            or _result_sha256(result) != entry.result_sha256
            or result.model != entry.model
        ):
            return self._failed(tool_call_id, request, "replay_source_changed", started_at, started)
        return ToolObservation[CtoxToolResult](
            tool_call_id=tool_call_id,
            request_id=request.request_id,
            run_id=request.run_id,
            status=ToolObservationStatus.SUCCEEDED,
            admission=self._admission(),
            result=result,
            raw_result=ArtifactReference(
                artifact_id=entry.source_tool_call_id,
                schema_name="ctoxpred2_tool_result",
                schema_version=result.schema_version,
                media_type="application/json",
                sha256=entry.result_sha256,
            ),
            execution_metadata=self._metadata(request, started_at, started, snapshot),
        )

    def _failed(
        self,
        tool_call_id: UUID,
        request: ToolRequest[CtoxToolArguments],
        code: str,
        started_at: datetime,
        started: float,
    ) -> ToolObservation[CtoxToolResult]:
        return ToolObservation[CtoxToolResult](
            tool_call_id=tool_call_id,
            request_id=request.request_id,
            run_id=request.run_id,
            status=ToolObservationStatus.FAILED,
            admission=self._admission(),
            error=ExecutionError(
                code=code, message="저장된 CToxPred2 관측을 replay하지 못했습니다.", retryable=False
            ),
            execution_metadata=self._metadata(request, started_at, started, None),
        )

    def _admission(self) -> ToolAdmission:
        return ToolAdmission(
            decision=ToolAdmissionDecision.APPROVED, policy_version=self.POLICY_VERSION
        )

    def _metadata(
        self,
        request: ToolRequest[CtoxToolArguments],
        started_at: datetime,
        started: float,
        snapshot: CtoxObservationSnapshot | None,
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
        return ExecutionMetadata(
            started_at=started_at,
            finished_at=datetime.now(UTC),
            duration_ms=int((perf_counter() - started) * 1000),
            implementation_version="ctoxpred2-observation-replay-v1",
            components=tuple(components),
            usage=ExecutionUsage(),
        )
