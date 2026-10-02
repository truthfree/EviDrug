"""조건부 UPDATE와 고유 제약으로 동시 요청의 예산을 원자 예약한다."""

from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.execution_contracts.tool import ToolAdmission, ToolAdmissionDecision
from evidrug_api.tool_admission.contracts import RunContext, ToolRunPolicy
from evidrug_api.tool_admission.tables import ToolAdmissionRecord, ToolRunRecord
from evidrug_api.tool_execution.repository import database_now
from evidrug_api.tool_execution.runner import input_fingerprint


class AdmissionRepository:
    """호출 전용 session을 사용한다. 설정과 예약을 각각 commit한다."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def start_run(self, policy: ToolRunPolicy) -> None:
        """같은 run은 동일 설정만 재사용할 수 있다. 재시작 시 예산을 초기화하지 않는다."""
        digest = input_fingerprint(policy.model_dump(mode="json"))
        existing = await self.session.get(ToolRunRecord, policy.context.run_id)
        if existing is None:
            self.session.add(
                ToolRunRecord(
                    run_id=policy.context.run_id,
                    analysis_id=policy.context.analysis_id,
                    policy_json=policy.model_dump_json(),
                    policy_sha256=digest,
                    max_calls=policy.max_tool_calls,
                    used_calls=0,
                    deadline=policy.deadline,
                )
            )
            try:
                await self.session.commit()
                return
            except IntegrityError:
                await self.session.rollback()
                existing = await self.session.get(ToolRunRecord, policy.context.run_id)
                if existing is None:
                    raise
        if existing.policy_sha256 != digest:
            raise ValueError("run policy is immutable")

    async def policy(self, context: RunContext) -> ToolRunPolicy:
        """서버 context와 저장된 run 소유권이 일치해야 한다."""
        row = await self.session.get(ToolRunRecord, context.run_id)
        if row is None:
            raise ValueError("run is not initialized")
        policy = ToolRunPolicy.model_validate_json(row.policy_json)
        if policy.context != context:
            raise ValueError("run context mismatch")
        return policy

    async def decide(
        self,
        policy: ToolRunPolicy,
        *,
        request_id: UUID,
        tool_call_id: UUID,
        fingerprint: str,
        rejection: str | None,
        tool_id: str,
        tool_version: str,
    ) -> tuple[ToolAdmission, bool]:
        """승인과 예산 증가를 함께 저장한다. 거부·재전달은 예산을 추가 소비하지 않는다."""
        existing = await self._existing(policy, request_id, tool_call_id, fingerprint)
        if existing is not None:
            return existing, False
        now = await database_now(self.session)
        if rejection is None:
            reserved = await self.session.scalar(
                update(ToolRunRecord)
                .where(
                    ToolRunRecord.run_id == policy.context.run_id,
                    ToolRunRecord.used_calls < ToolRunRecord.max_calls,
                    ToolRunRecord.deadline > now,
                )
                .values(used_calls=ToolRunRecord.used_calls + 1)
                .returning(ToolRunRecord.run_id)
            )
            if reserved is None:
                rejection = (
                    "deadline_exceeded" if policy.deadline <= now else "tool_budget_exhausted"
                )
        admission = self._verdict(policy, rejection)
        self.session.add(
            ToolAdmissionRecord(
                request_id=request_id,
                tool_call_id=tool_call_id,
                run_id=policy.context.run_id,
                request_sha256=fingerprint,
                tool_id=tool_id,
                tool_version=tool_version,
                decision=admission.decision.value,
                reason_code=admission.reason_code,
                created_at=now,
            )
        )
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._existing(policy, request_id, tool_call_id, fingerprint)
            if existing is None:
                raise
            return existing, False
        return admission, admission.decision is ToolAdmissionDecision.APPROVED

    async def _existing(
        self,
        policy: ToolRunPolicy,
        request_id: UUID,
        tool_call_id: UUID,
        fingerprint: str,
    ) -> ToolAdmission | None:
        row = await self.session.scalar(
            select(ToolAdmissionRecord).where(
                or_(
                    ToolAdmissionRecord.request_id == request_id,
                    ToolAdmissionRecord.tool_call_id == tool_call_id,
                )
            )
        )
        if row is None:
            return None
        if (
            row.request_id != request_id
            or row.tool_call_id != tool_call_id
            or row.run_id != policy.context.run_id
            or row.request_sha256 != fingerprint
        ):
            return self._verdict(policy, "request_conflict")
        return self._verdict(policy, row.reason_code)

    @staticmethod
    def _verdict(policy: ToolRunPolicy, reason: str | None) -> ToolAdmission:
        return ToolAdmission(
            decision=ToolAdmissionDecision.REJECTED if reason else ToolAdmissionDecision.APPROVED,
            policy_version=policy.policy_version,
            reason_code=reason,
        )
