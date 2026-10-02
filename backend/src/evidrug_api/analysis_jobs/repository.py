"""분석 작업을 PostgreSQL에 원자적으로 저장하고 조회하는 계층."""

from typing import cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import Select

from evidrug_api.analysis_input.models import AnalysisInputResponse
from evidrug_api.analysis_jobs.models import (
    AnalysisEventType,
    AnalysisStageName,
    AnalysisStatus,
)
from evidrug_api.analysis_jobs.tables import (
    AnalysisEventRecord,
    AnalysisRecord,
    AnalysisStageRecord,
)
from evidrug_api.orchestration.tables import AgentReplayRecord, AgentRunRecord


class AnalysisRepository:
    """분석 생성의 멱등성과 상태 event 저장을 관리한다."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def find_by_idempotency(
        self, session_fingerprint: str, idempotency_key: str
    ) -> AnalysisRecord | None:
        statement = self._loaded_analysis().where(
            AnalysisRecord.session_fingerprint == session_fingerprint,
            AnalysisRecord.idempotency_key == idempotency_key,
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def get(self, analysis_id: UUID) -> AnalysisRecord | None:
        statement = self._loaded_analysis().where(AnalysisRecord.id == analysis_id)
        return (await self.session.scalars(statement)).one_or_none()

    async def list_recent(
        self, session_fingerprint: str, visitor_fingerprint: str | None, limit: int = 20
    ) -> tuple[AnalysisRecord, ...]:
        """현재 세션 또는 같은 브라우저의 일반 분석만 최신순으로 읽는다."""

        owner_filter = AnalysisRecord.session_fingerprint == session_fingerprint
        if visitor_fingerprint is not None:
            owner_filter = or_(
                owner_filter, AnalysisRecord.visitor_fingerprint == visitor_fingerprint
            )
        replay_exists = (
            select(AgentReplayRecord.analysis_id)
            .where(AgentReplayRecord.analysis_id == AnalysisRecord.id)
            .exists()
        )
        rows = await self.session.scalars(
            select(AnalysisRecord)
            .where(owner_filter, ~replay_exists)
            .order_by(AnalysisRecord.created_at.desc(), AnalysisRecord.id.desc())
            .limit(limit)
        )
        return tuple(rows)

    async def get_latest_target_output_json(self, analysis_id: UUID) -> str | None:
        """완료된 최신 Target run의 원본 JSON을 API projection용으로 조회한다."""
        return await self.session.scalar(
            select(AgentRunRecord.output_json)
            .where(
                AgentRunRecord.analysis_id == analysis_id,
                AgentRunRecord.agent_name == AnalysisStageName.TARGET_HYPOTHESIS,
                AgentRunRecord.status.in_(("completed", "partial_failure")),
            )
            .order_by(AgentRunRecord.attempt.desc())
            .limit(1)
        )

    async def is_development_replay(self, analysis_id: UUID) -> bool:
        """CLI 분석은 세션 fingerprint와 무관하게 공개 조회에서 제외한다."""
        return await self.session.get(AgentReplayRecord, analysis_id) is not None

    async def get_latest_run(
        self, analysis_id: UUID, agent_name: AnalysisStageName
    ) -> AgentRunRecord | None:
        """최신 attempt를 상태와 무관하게 읽어 과거 성공값으로의 대체를 막는다."""
        row = await self.session.scalar(
            select(AgentRunRecord)
            .where(
                AgentRunRecord.analysis_id == analysis_id,
                AgentRunRecord.agent_name == agent_name,
            )
            .order_by(AgentRunRecord.attempt.desc())
            .limit(1)
        )
        return row

    async def get_run_attempt(
        self, analysis_id: UUID, agent_name: AnalysisStageName, attempt: int
    ) -> AgentRunRecord | None:
        """baseline과 recall을 섞지 않고 정확한 Agent 시도를 조회한다."""
        return cast(
            AgentRunRecord | None,
            await self.session.scalar(
                select(AgentRunRecord).where(
                    AgentRunRecord.analysis_id == analysis_id,
                    AgentRunRecord.agent_name == agent_name,
                    AgentRunRecord.attempt == attempt,
                )
            ),
        )

    async def list_agent_calls(self, analysis_id: UUID) -> tuple[AgentRunRecord, ...]:
        """모든 Agent 호출을 단계와 단계 내 호출 번호 순으로 읽는다."""
        rows = await self.session.scalars(
            select(AgentRunRecord)
            .where(AgentRunRecord.analysis_id == analysis_id)
            .order_by(AgentRunRecord.agent_name, AgentRunRecord.attempt)
        )
        return tuple(rows)

    async def create(
        self,
        payload: AnalysisInputResponse,
        session_fingerprint: str,
        idempotency_key: str,
        visitor_fingerprint: str | None = None,
    ) -> tuple[AnalysisRecord, bool]:
        existing = await self.find_by_idempotency(session_fingerprint, idempotency_key)
        if existing is not None:
            return existing, False

        record = AnalysisRecord(
            session_fingerprint=session_fingerprint,
            visitor_fingerprint=visitor_fingerprint,
            idempotency_key=idempotency_key,
            disease_id=payload.disease_id,
            disease_name=payload.disease_name,
            target_mode=payload.target_mode,
            target_name=payload.target_name,
            original_smiles=payload.original_smiles,
            canonical_smiles=payload.canonical_smiles,
            potency_endpoint=(
                payload.potency_criterion.endpoint.value
                if payload.potency_criterion is not None
                else None
            ),
            potency_maximum_value=(
                payload.potency_criterion.maximum_value
                if payload.potency_criterion is not None
                else None
            ),
            potency_unit=(
                payload.potency_criterion.unit if payload.potency_criterion is not None else None
            ),
            stages=[
                AnalysisStageRecord(name=name, position=position)
                for position, name in enumerate(AnalysisStageName, start=1)
            ],
            events=[
                AnalysisEventRecord(
                    event_type=AnalysisEventType.CREATED,
                    status=AnalysisStatus.QUEUED,
                )
            ],
        )
        self.session.add(record)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            duplicate = await self.find_by_idempotency(session_fingerprint, idempotency_key)
            if duplicate is None:
                raise
            return duplicate, False

        created = await self.get(record.id)
        if created is None:  # pragma: no cover - database invariant
            raise RuntimeError("created analysis could not be reloaded")
        return created, True

    async def mark_dispatch_failed(self, analysis_id: UUID) -> AnalysisRecord:
        record = await self.get(analysis_id)
        if record is None:  # pragma: no cover - service passes a persisted ID
            raise RuntimeError("analysis disappeared before dispatch failure was saved")
        record.status = AnalysisStatus.FAILED
        record.error_code = "queue_unavailable"
        record.events.append(
            AnalysisEventRecord(
                event_type=AnalysisEventType.DISPATCH_FAILED,
                status=AnalysisStatus.FAILED,
                reason_code="queue_unavailable",
            )
        )
        await self.session.commit()
        failed = await self.get(analysis_id)
        if failed is None:  # pragma: no cover - database invariant
            raise RuntimeError("failed analysis could not be reloaded")
        return failed

    @staticmethod
    def _loaded_analysis() -> Select[tuple[AnalysisRecord]]:
        return select(AnalysisRecord).options(
            selectinload(AnalysisRecord.stages),
            selectinload(AnalysisRecord.events),
        )
