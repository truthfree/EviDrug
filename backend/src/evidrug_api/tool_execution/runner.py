"""실행 전 commit과 결과/최종 상태의 원자 저장을 연결한다."""

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.tool_execution.repository import (
    ExecutionAlreadyFailed,
    ExecutionIdentity,
    ExecutionLeaseLost,
    ExecutionRepository,
)

logger = logging.getLogger(__name__)


def input_fingerprint(payload: object) -> str:
    """입력과 구현 식별자를 원문 대신 hash로 ledger에 보존한다."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def failure_code(error: BaseException) -> str:
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, TimeoutError):
        return "provider_timeout"
    if isinstance(error, ExecutionLeaseLost):
        return "lease_expired"
    if isinstance(error, SQLAlchemyError):
        return "persistence_failed"
    code = getattr(error, "code", None)
    if code in ("provider_timeout", "provider_unavailable"):
        return str(code)
    return "invalid_result" if isinstance(error, ValueError) else "execution_failed"


class ToolExecutionRunner:
    """호출별 독립 session을 사용한다. timeout 후 30초 여유를 둔 고정 lease다."""

    def __init__(self, session: AsyncSession, *, timeout_seconds: float = 300) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout must be positive and finite")
        self.session = session
        self.timeout_seconds = timeout_seconds

    async def execute[T](
        self,
        identity: ExecutionIdentity,
        *,
        invoke: Callable[[], Awaitable[T]],
        load: Callable[[], Awaitable[T | None]],
        save: Callable[[T], Awaitable[None]],
        outcome_error: Callable[[T], str | None],
    ) -> T:
        """소유자만 실행한다. save는 commit 없이 결과를 stage해야 한다."""
        ledger = ExecutionRepository(self.session)
        claim = await ledger.claim(identity, lease_seconds=self.timeout_seconds + 30)
        if not claim.acquired:
            stored = await load()
            if stored is not None and outcome_error(stored) == claim.error_code:
                return stored
            raise ExecutionAlreadyFailed(claim.error_code or "stored_result_missing")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                # 기존 migration 이전 완료 결과도 재추론 없이 ledger에 편입한다.
                result = await load()
                await self.session.commit()
                if result is None:
                    result = await invoke()
            for attempt in range(2):
                try:
                    await ledger.finish(identity, claim, error_code=outcome_error(result))
                    await save(result)
                    await self.session.commit()
                    break
                except IntegrityError:
                    # 서로 다른 호출이 공유 catalog를 동시에 생성할 수 있다.
                    # 추론은 반복하지 않고 결과 transaction만 한 번 재시도한다.
                    await self.session.rollback()
                    if attempt == 1:
                        raise
            return result
        except (Exception, asyncio.CancelledError) as error:
            await self.session.rollback()
            try:
                await ledger.fail(identity, claim, failure_code(error))
            except SQLAlchemyError:
                await self.session.rollback()
                # DB 장애로 실패 저장도 불가능하면 다음 회수 실행이 lease 만료를 처리한다.
                logger.error(
                    "Could not persist tool failure",
                    extra={"tool_call_id": str(identity.tool_call_id)},
                )
            raise
