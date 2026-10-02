"""DB 고유 제약과 조건부 갱신으로 실행 소유권을 관리한다."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.tool_execution.tables import ToolExecutionRecord


class ExecutionConflict(RuntimeError):
    """같은 식별자를 다른 요청에 사용했다."""


class ExecutionInProgress(RuntimeError):
    """다른 호출이 실행 소유권을 보유하고 있다."""


class ExecutionLeaseLost(RuntimeError):
    """만료되거나 회수된 소유권으로는 결과를 저장할 수 없다."""


class ExecutionAlreadyFailed(RuntimeError):
    """실패한 호출은 새 ID로 명시적으로 재시도해야 한다."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExecutionIdentity:
    tool_call_id: UUID
    request_id: UUID
    analysis_id: UUID
    run_id: UUID
    tool_id: str
    input_sha256: str


@dataclass(frozen=True)
class ExecutionClaim:
    owner_token: UUID
    acquired: bool
    status: str
    error_code: str | None


async def database_now(session: AsyncSession) -> datetime:
    """worker 사이의 시계 차이를 피하도록 DB 시간을 사용한다."""
    value = await session.scalar(select(func.current_timestamp()))
    if not isinstance(value, datetime):
        raise RuntimeError("database did not return a timestamp")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class ExecutionRepository:
    """호출 전용 session에서 이용하며 claim과 회수는 자체 commit한다."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(self, identity: ExecutionIdentity, *, lease_seconds: float) -> ExecutionClaim:
        """추론 전에 running을 commit한다. 같은 ID의 소유권 재취득은 허용하지 않는다."""
        await self.recover_expired()
        existing = await self._existing(identity)
        if existing is not None:
            return existing
        now = await database_now(self.session)
        owner = uuid4()
        self.session.add(
            ToolExecutionRecord(
                tool_call_id=identity.tool_call_id,
                request_id=identity.request_id,
                analysis_id=identity.analysis_id,
                run_id=identity.run_id,
                tool_id=identity.tool_id,
                input_sha256=identity.input_sha256,
                owner_token=owner,
                status="running",
                started_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
        )
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._existing(identity)
            if existing is None:
                raise
            return existing
        return ExecutionClaim(owner, True, "running", None)

    async def _existing(self, identity: ExecutionIdentity) -> ExecutionClaim | None:
        row = await self.session.scalar(
            select(ToolExecutionRecord)
            .where(
                or_(
                    ToolExecutionRecord.tool_call_id == identity.tool_call_id,
                    ToolExecutionRecord.request_id == identity.request_id,
                )
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            return None
        stored = ExecutionIdentity(
            row.tool_call_id,
            row.request_id,
            row.analysis_id,
            row.run_id,
            row.tool_id,
            row.input_sha256,
        )
        if stored != identity:
            raise ExecutionConflict("execution identifier has different content")
        if row.status == "running":
            raise ExecutionInProgress("execution is already running")
        return ExecutionClaim(row.owner_token, False, row.status, row.error_code)

    async def finish(
        self, identity: ExecutionIdentity, claim: ExecutionClaim, *, error_code: str | None
    ) -> None:
        """결과 저장과 같은 transaction에서 소유권과 만료를 확인한다. commit하지 않는다."""
        now = await database_now(self.session)
        statement = (
            update(ToolExecutionRecord)
            .where(
                ToolExecutionRecord.tool_call_id == identity.tool_call_id,
                ToolExecutionRecord.owner_token == claim.owner_token,
                ToolExecutionRecord.status == "running",
                ToolExecutionRecord.lease_expires_at > now,
            )
            .values(
                status="failed" if error_code else "succeeded",
                error_code=error_code,
                finished_at=now,
            )
            .returning(ToolExecutionRecord.tool_call_id)
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise ExecutionLeaseLost("execution lease expired or was recovered")

    async def fail(self, identity: ExecutionIdentity, claim: ExecutionClaim, code: str) -> None:
        """원래 오류를 고정 코드로 기록한다. 이미 회수된 terminal 행은 덮어쓰지 않는다."""
        now = await database_now(self.session)
        await self.session.execute(
            update(ToolExecutionRecord)
            .where(
                ToolExecutionRecord.tool_call_id == identity.tool_call_id,
                ToolExecutionRecord.owner_token == claim.owner_token,
                ToolExecutionRecord.status == "running",
            )
            .values(status="failed", error_code=code, finished_at=now)
        )
        await self.session.commit()

    async def recover_expired(self) -> int:
        """만료된 running 행을 실패로 회수한다. 재추론이나 자동 재시도는 하지 않는다."""
        now = await database_now(self.session)
        rows = await self.session.scalars(
            update(ToolExecutionRecord)
            .where(
                ToolExecutionRecord.status == "running",
                ToolExecutionRecord.lease_expires_at <= now,
            )
            .values(status="failed", error_code="lease_expired", finished_at=now)
            .returning(ToolExecutionRecord.tool_call_id)
        )
        count = len(rows.all())
        await self.session.commit()
        return count
