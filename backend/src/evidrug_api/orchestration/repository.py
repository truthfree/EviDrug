"""DB 조건부 갱신으로 orchestration 소유권과 단계 상태를 관리한다."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from evidrug_api.analysis_input.models import (
    AnalysisInputResponse,
    PotencyCriterion,
    PotencyEndpoint,
)
from evidrug_api.analysis_jobs.models import (
    AnalysisEventType,
    AnalysisStageName,
    AnalysisStageStatus,
    AnalysisStatus,
)
from evidrug_api.analysis_jobs.tables import (
    AnalysisEventRecord,
    AnalysisRecord,
    AnalysisStageRecord,
)
from evidrug_api.execution_contracts.agent import AgentOutput, AgentOutputStatus
from evidrug_api.execution_contracts.common import ArtifactReference
from evidrug_api.orchestration.contracts import ExecutionProfile, TraceEventType
from evidrug_api.orchestration.tables import (
    AgentRunRecord,
    AnalysisExecutionRecord,
    ExecutionTraceRecord,
)
from evidrug_api.tool_execution.repository import database_now


class AnalysisNotRunnable(RuntimeError):
    """분석이 없거나 queue 전달 전에 이미 terminal 상태가 됐다."""


class OrchestrationInProgress(RuntimeError):
    """다른 worker가 아직 유효한 분석 실행 소유권을 보유한다."""


class OrchestrationLeaseLost(RuntimeError):
    """만료되거나 회수된 분석 실행은 상태를 더 변경할 수 없다."""


@dataclass(frozen=True)
class AnalysisClaim:
    """분석 실행 소유권과 downstream 입력 snapshot."""

    analysis_id: UUID
    owner_token: UUID
    acquired: bool
    execution_status: str
    analysis_status: AnalysisStatus
    case_input: AnalysisInputResponse


class OrchestrationRepository:
    """각 메서드가 짧은 transaction 안에서 현재 상태와 trace를 함께 저장한다."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(
        self,
        analysis_id: UUID,
        profile: ExecutionProfile,
        *,
        lease_seconds: int,
    ) -> AnalysisClaim:
        """queued 분석을 원자적으로 running으로 만들고 실행 정책을 고정한다."""
        await self.recover_expired()
        existing = await self.session.get(AnalysisExecutionRecord, analysis_id)
        if existing is not None:
            return await self._existing_claim(existing, profile)

        analysis = await self._analysis(analysis_id)
        if analysis is None or analysis.status is not AnalysisStatus.QUEUED:
            raise AnalysisNotRunnable(str(analysis_id))

        now = await database_now(self.session)
        owner_token = uuid4()
        profile_json = profile.model_dump_json()
        profile_sha256 = hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
        self.session.add(
            AnalysisExecutionRecord(
                analysis_id=analysis_id,
                owner_token=owner_token,
                status="running",
                profile_json=profile_json,
                profile_sha256=profile_sha256,
                started_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
        )
        analysis.status = AnalysisStatus.RUNNING
        analysis.error_code = None
        analysis.events.append(
            AnalysisEventRecord(
                event_type=AnalysisEventType.STARTED,
                status=AnalysisStatus.RUNNING,
                created_at=now,
            )
        )
        self.session.add(
            ExecutionTraceRecord(
                analysis_id=analysis_id,
                event_type=TraceEventType.ANALYSIS_STARTED,
                policy_version=profile.policy_version,
                created_at=now,
            )
        )
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self.session.get(AnalysisExecutionRecord, analysis_id)
            if existing is None:
                raise
            return await self._existing_claim(existing, profile)
        return AnalysisClaim(
            analysis_id=analysis_id,
            owner_token=owner_token,
            acquired=True,
            execution_status="running",
            analysis_status=AnalysisStatus.RUNNING,
            case_input=self._case_input(analysis),
        )

    async def start_stage(
        self,
        *,
        analysis_id: UUID,
        owner_token: UUID,
        run_id: UUID,
        agent_name: AnalysisStageName,
        input_sha256: str,
        policy_version: str,
        input_json: str | None = None,
        parent_run_id: UUID | None = None,
        attempt: int = 1,
    ) -> None:
        """Agent 호출 전에 run과 stage의 running 상태를 commit한다."""
        now = await self._require_active(analysis_id, owner_token)
        stage = await self._stage(analysis_id, agent_name)
        allowed_statuses = (
            {AnalysisStageStatus.PENDING} if attempt == 1 else {AnalysisStageStatus.COMPLETED}
        )
        if (
            stage is None
            or stage.status not in allowed_statuses
            or attempt not in (1, 2)
            or (attempt == 2 and parent_run_id is None)
        ):
            raise RuntimeError(f"stage is not pending: {agent_name}")
        if attempt == 2:
            parent = await self.session.get(AgentRunRecord, parent_run_id)
            previous = await self.session.scalar(
                select(AgentRunRecord).where(
                    AgentRunRecord.analysis_id == analysis_id,
                    AgentRunRecord.agent_name == agent_name,
                    AgentRunRecord.attempt == 1,
                )
            )
            if (
                agent_name not in {AnalysisStageName.ADMET, AnalysisStageName.DECISION}
                or parent is None
                or parent.analysis_id != analysis_id
                or parent.agent_name != AnalysisStageName.DECISION
                or parent.attempt != 1
                or parent.status not in {"completed", "partial_failure"}
                or previous is None
                or previous.status not in {"completed", "partial_failure"}
            ):
                raise RuntimeError("invalid recall parent or previous agent run")
        stage.status = AnalysisStageStatus.RUNNING
        self.session.add(
            AgentRunRecord(
                run_id=run_id,
                analysis_id=analysis_id,
                agent_name=agent_name,
                attempt=attempt,
                status="running",
                input_sha256=input_sha256,
                input_json=input_json,
                parent_run_id=parent_run_id,
                started_at=now,
            )
        )
        self.session.add(
            ExecutionTraceRecord(
                analysis_id=analysis_id,
                run_id=run_id,
                event_type=TraceEventType.STAGE_STARTED,
                stage_name=agent_name,
                policy_version=policy_version,
                created_at=now,
            )
        )
        await self.session.commit()

    async def finish_stage(
        self,
        *,
        analysis_id: UUID,
        owner_token: UUID,
        output: AgentOutput[BaseModel],
        policy_version: str,
    ) -> ArtifactReference | None:
        """검증된 AgentOutput과 단계 terminal 상태를 함께 저장한다."""
        now = await self._require_active(analysis_id, owner_token)
        run = await self.session.get(AgentRunRecord, output.run_id)
        stage = await self._stage(analysis_id, output.agent_name)
        if run is None or stage is None or run.status != "running":
            raise RuntimeError("agent run is not running")

        output_json = output.model_dump_json()
        output_sha256 = hashlib.sha256(output_json.encode("utf-8")).hexdigest()
        run.status = output.status.value
        run.output_json = output_json
        run.output_sha256 = output_sha256
        run.execution_metadata_json = output.execution_metadata.model_dump_json()
        run.error_code = output.error.code if output.error else None
        run.finished_at = now
        stage.status = self._stage_status(output.status)

        event_type = {
            AgentOutputStatus.COMPLETED: TraceEventType.STAGE_COMPLETED,
            AgentOutputStatus.PARTIAL_FAILURE: TraceEventType.STAGE_PARTIAL_FAILURE,
            AgentOutputStatus.FAILED: TraceEventType.STAGE_FAILED,
            AgentOutputStatus.SKIPPED: TraceEventType.STAGE_SKIPPED,
        }[output.status]
        self.session.add(
            ExecutionTraceRecord(
                analysis_id=analysis_id,
                run_id=output.run_id,
                event_type=event_type,
                stage_name=output.agent_name,
                reason_code=run.error_code,
                policy_version=policy_version,
                created_at=now,
            )
        )
        await self.session.commit()

        if output.status not in {
            AgentOutputStatus.COMPLETED,
            AgentOutputStatus.PARTIAL_FAILURE,
        }:
            return None
        return ArtifactReference(
            artifact_id=output.run_id,
            schema_name="agent_output",
            schema_version=output.schema_version,
            media_type="application/json",
            sha256=output_sha256,
        )

    async def fail_stage(
        self,
        *,
        analysis_id: UUID,
        owner_token: UUID,
        run_id: UUID,
        agent_name: AnalysisStageName,
        error_code: str,
        policy_version: str,
        skipped: bool = False,
    ) -> None:
        """executor 예외 또는 기계적인 선행 입력 누락을 고정 코드로 저장한다."""
        now = await self._require_active(analysis_id, owner_token)
        run = await self.session.get(AgentRunRecord, run_id)
        stage = await self._stage(analysis_id, agent_name)
        if run is None or stage is None or run.status != "running":
            raise RuntimeError("agent run is not running")
        run.status = "skipped" if skipped else "failed"
        run.error_code = error_code
        run.finished_at = now
        stage.status = AnalysisStageStatus.SKIPPED if skipped else AnalysisStageStatus.FAILED
        self.session.add(
            ExecutionTraceRecord(
                analysis_id=analysis_id,
                run_id=run_id,
                event_type=(
                    TraceEventType.STAGE_SKIPPED if skipped else TraceEventType.STAGE_FAILED
                ),
                stage_name=agent_name,
                reason_code=error_code,
                policy_version=policy_version,
                created_at=now,
            )
        )
        await self.session.commit()

    async def finish_analysis(
        self,
        *,
        analysis_id: UUID,
        owner_token: UUID,
        status: AnalysisStatus,
        policy_version: str,
        error_code: str | None = None,
    ) -> None:
        """최종 분석 상태와 orchestration ledger를 원자적으로 종료한다."""
        now = await self._require_active(analysis_id, owner_token)
        analysis = await self._analysis(analysis_id)
        execution = await self.session.get(AnalysisExecutionRecord, analysis_id)
        if analysis is None or execution is None:
            raise RuntimeError("analysis disappeared during orchestration")
        analysis.status = status
        analysis.error_code = error_code
        execution.status = "failed" if status is AnalysisStatus.FAILED else "succeeded"
        execution.error_code = error_code if status is AnalysisStatus.FAILED else None
        execution.finished_at = now
        event_type = {
            AnalysisStatus.COMPLETED: AnalysisEventType.COMPLETED,
            AnalysisStatus.PARTIAL_FAILURE: AnalysisEventType.PARTIAL_FAILURE,
            AnalysisStatus.FAILED: AnalysisEventType.FAILED,
        }[status]
        trace_type = {
            AnalysisStatus.COMPLETED: TraceEventType.ANALYSIS_COMPLETED,
            AnalysisStatus.PARTIAL_FAILURE: TraceEventType.ANALYSIS_PARTIAL_FAILURE,
            AnalysisStatus.FAILED: TraceEventType.ANALYSIS_FAILED,
        }[status]
        analysis.events.append(
            AnalysisEventRecord(
                event_type=event_type,
                status=status,
                reason_code=error_code,
                created_at=now,
            )
        )
        self.session.add(
            ExecutionTraceRecord(
                analysis_id=analysis_id,
                event_type=trace_type,
                reason_code=error_code,
                policy_version=policy_version,
                created_at=now,
            )
        )
        await self.session.commit()

    async def recover_expired(self) -> int:
        """만료 실행과 남은 단계를 terminal 상태로 만들되 자동 재실행하지 않는다."""
        now = await database_now(self.session)
        expired_ids = list(
            await self.session.scalars(
                update(AnalysisExecutionRecord)
                .where(
                    AnalysisExecutionRecord.status == "running",
                    AnalysisExecutionRecord.lease_expires_at <= now,
                )
                .values(status="failed", error_code="lease_expired", finished_at=now)
                .returning(AnalysisExecutionRecord.analysis_id)
            )
        )
        for analysis_id in expired_ids:
            execution = await self.session.get(AnalysisExecutionRecord, analysis_id)
            analysis = await self._analysis(analysis_id)
            if analysis is None or execution is None:
                continue
            analysis.status = AnalysisStatus.FAILED
            analysis.error_code = "lease_expired"
            analysis.events.append(
                AnalysisEventRecord(
                    event_type=AnalysisEventType.LEASE_EXPIRED,
                    status=AnalysisStatus.FAILED,
                    reason_code="lease_expired",
                    created_at=now,
                )
            )
            running_runs = list(
                await self.session.scalars(
                    select(AgentRunRecord).where(
                        AgentRunRecord.analysis_id == execution.analysis_id,
                        AgentRunRecord.status == "running",
                    )
                )
            )
            for run in running_runs:
                run.status = "failed"
                run.error_code = "lease_expired"
                run.finished_at = now
                self.session.add(
                    ExecutionTraceRecord(
                        analysis_id=execution.analysis_id,
                        run_id=run.run_id,
                        event_type=TraceEventType.STAGE_FAILED,
                        stage_name=run.agent_name,
                        reason_code="lease_expired",
                        policy_version=self._policy_version(execution),
                        created_at=now,
                    )
                )
            for stage in analysis.stages:
                if stage.status is AnalysisStageStatus.RUNNING:
                    stage.status = AnalysisStageStatus.FAILED
                elif stage.status is AnalysisStageStatus.PENDING:
                    stage.status = AnalysisStageStatus.SKIPPED
                    self.session.add(
                        ExecutionTraceRecord(
                            analysis_id=execution.analysis_id,
                            event_type=TraceEventType.STAGE_SKIPPED,
                            stage_name=stage.name,
                            reason_code="lease_expired",
                            policy_version=self._policy_version(execution),
                            created_at=now,
                        )
                    )
            self.session.add(
                ExecutionTraceRecord(
                    analysis_id=execution.analysis_id,
                    event_type=TraceEventType.LEASE_EXPIRED,
                    reason_code="lease_expired",
                    policy_version=self._policy_version(execution),
                    created_at=now,
                )
            )
        if expired_ids:
            await self.session.commit()
        return len(expired_ids)

    async def _existing_claim(
        self, execution: AnalysisExecutionRecord, profile: ExecutionProfile
    ) -> AnalysisClaim:
        expected = hashlib.sha256(profile.model_dump_json().encode("utf-8")).hexdigest()
        if execution.profile_sha256 != expected:
            raise AnalysisNotRunnable("analysis was started with a different execution profile")
        if execution.status == "running":
            raise OrchestrationInProgress(str(execution.analysis_id))
        analysis = await self._analysis(execution.analysis_id)
        if analysis is None:
            raise AnalysisNotRunnable(str(execution.analysis_id))
        return AnalysisClaim(
            analysis_id=execution.analysis_id,
            owner_token=execution.owner_token,
            acquired=False,
            execution_status=execution.status,
            analysis_status=analysis.status,
            case_input=self._case_input(analysis),
        )

    async def _require_active(self, analysis_id: UUID, owner_token: UUID) -> datetime:
        now = await database_now(self.session)
        active = await self.session.scalar(
            select(AnalysisExecutionRecord.analysis_id).where(
                AnalysisExecutionRecord.analysis_id == analysis_id,
                AnalysisExecutionRecord.owner_token == owner_token,
                AnalysisExecutionRecord.status == "running",
                AnalysisExecutionRecord.lease_expires_at > now,
            )
        )
        if active is None:
            raise OrchestrationLeaseLost(str(analysis_id))
        return now

    async def _analysis(self, analysis_id: UUID) -> AnalysisRecord | None:
        return cast(
            AnalysisRecord | None,
            await self.session.scalar(
                select(AnalysisRecord)
                .where(AnalysisRecord.id == analysis_id)
                .options(selectinload(AnalysisRecord.stages), selectinload(AnalysisRecord.events))
                .execution_options(populate_existing=True)
            ),
        )

    async def _stage(
        self, analysis_id: UUID, name: AnalysisStageName
    ) -> AnalysisStageRecord | None:
        return cast(
            AnalysisStageRecord | None,
            await self.session.scalar(
                select(AnalysisStageRecord).where(
                    AnalysisStageRecord.analysis_id == analysis_id,
                    AnalysisStageRecord.name == name,
                )
            ),
        )

    @staticmethod
    def _case_input(analysis: AnalysisRecord) -> AnalysisInputResponse:
        return AnalysisInputResponse(
            disease_id=analysis.disease_id,
            disease_name=analysis.disease_name,
            target_mode=analysis.target_mode,
            target_name=analysis.target_name,
            original_smiles=analysis.original_smiles,
            canonical_smiles=analysis.canonical_smiles,
            potency_criterion=(
                PotencyCriterion(
                    endpoint=PotencyEndpoint(analysis.potency_endpoint),
                    maximum_value=cast(float, analysis.potency_maximum_value),
                    unit=cast(str, analysis.potency_unit),
                )
                if analysis.potency_endpoint is not None
                else None
            ),
        )

    @staticmethod
    def _stage_status(status: AgentOutputStatus) -> AnalysisStageStatus:
        return {
            AgentOutputStatus.COMPLETED: AnalysisStageStatus.COMPLETED,
            AgentOutputStatus.PARTIAL_FAILURE: AnalysisStageStatus.COMPLETED,
            AgentOutputStatus.FAILED: AnalysisStageStatus.FAILED,
            AgentOutputStatus.SKIPPED: AnalysisStageStatus.SKIPPED,
        }[status]

    @staticmethod
    def _policy_version(execution: AnalysisExecutionRecord) -> str:
        profile = ExecutionProfile.model_validate_json(execution.profile_json)
        return profile.policy_version
