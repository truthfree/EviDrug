"""승인된 요청만 등록 service로 전달하고 공통 관측으로 변환한다."""

from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.execution_contracts.common import (
    ComponentVersion,
    ExecutionError,
    ExecutionMetadata,
    ExecutionUsage,
)
from evidrug_api.execution_contracts.tool import (
    ToolAdmissionDecision,
    ToolObservation,
    ToolObservationStatus,
    ToolRequest,
)
from evidrug_api.tool_admission.contracts import RunContext, ToolRunPolicy
from evidrug_api.tool_admission.registry import Invocation, ToolRegistry
from evidrug_api.tool_admission.repository import AdmissionRepository
from evidrug_api.tool_execution.repository import (
    ExecutionAlreadyFailed,
    ExecutionInProgress,
    database_now,
)
from evidrug_api.tool_execution.runner import failure_code, input_fingerprint


class ToolOutcomeError(RuntimeError):
    """도메인 unavailable을 result 없는 failed observation으로 변환한다."""

    def __init__(self, code: str, *, external_requests: int = 0) -> None:
        self.code = code
        self.external_requests = external_requests
        super().__init__(code)


class AdmittedToolExecutor:
    """서버 전용 경계. 호출마다 독립 session을 사용하며 예외 원문은 공개하지 않는다."""

    def __init__(self, registry: ToolRegistry, session: AsyncSession) -> None:
        self.registry = registry
        self.session = session
        self.repository = AdmissionRepository(session)

    async def start_run(self, policy: ToolRunPolicy) -> None:
        """서버 설정을 검증·고정한다. LLM/HTTP payload에서 policy를 만들지 않는다."""
        self.registry.validate_policy(policy)
        await self.repository.start_run(policy)

    async def schemas(self, context: RunContext) -> tuple[dict[str, object], ...]:
        return self.registry.schemas(await self.repository.policy(context))

    async def execute[T: BaseModel](
        self,
        context: RunContext,
        *,
        tool_call_id: UUID,
        request: ToolRequest[T],
    ) -> ToolObservation[BaseModel]:
        """거부는 DB에 기록하고 provider를 호출하지 않는다. 취소는 호출자에게 전파한다."""
        started_at = datetime.now(UTC)
        started = perf_counter()
        policy = await self.repository.policy(context)
        binding, arguments, rejection = self.registry.check(
            policy,
            request.tool_id,
            request.tool_version,
            request.arguments,
        )
        if request.run_id != context.run_id:
            rejection = "run_mismatch"
        admission, newly_reserved = await self.repository.decide(
            policy,
            request_id=request.request_id,
            tool_call_id=tool_call_id,
            fingerprint=input_fingerprint(request.model_dump(mode="json", serialize_as_any=True)),
            rejection=rejection,
            tool_id=request.tool_id,
            tool_version=request.tool_version,
        )
        result: BaseModel | None = None
        external_requests = 0
        code = admission.reason_code
        if admission.decision is ToolAdmissionDecision.REJECTED:
            status = ToolObservationStatus.REJECTED
        else:
            status = ToolObservationStatus.FAILED
            # Registry가 배포 사이에 제거/변경되어도 기존 승인이 실행 권한을 넓히지 않는다.
            code = rejection
            if code is None and binding is not None and arguments is not None:
                remaining = (policy.deadline - await database_now(self.session)).total_seconds()
                await self.session.commit()
                if remaining <= 0:
                    code = "deadline_exceeded"
                else:
                    invocation = Invocation(
                        context,
                        request.request_id,
                        tool_call_id,
                        min(remaining, policy.call_timeout_seconds, binding.max_timeout_seconds),
                        self.session,
                    )
                    try:
                        output = await binding.invoke(invocation, arguments)
                        result = binding.result_type.model_validate(output.model_dump(mode="json"))
                        external_requests = int(getattr(result, "external_requests", 0))
                        status = ToolObservationStatus.SUCCEEDED
                    except ToolOutcomeError as error:
                        code = error.code
                        external_requests = error.external_requests
                    except ExecutionInProgress:
                        code = "execution_in_progress"
                    except ExecutionAlreadyFailed as error:
                        code = error.code
                    except Exception as error:
                        await self.session.rollback()
                        code = failure_code(error)
        return ToolObservation[BaseModel](
            tool_call_id=tool_call_id,
            request_id=request.request_id,
            run_id=context.run_id,
            status=status,
            admission=admission,
            result=result,
            error=ExecutionError(
                code=code or "execution_failed",
                message="도구 요청을 완료하지 못했습니다.",
                retryable=code
                in {"provider_timeout", "provider_unavailable", "execution_in_progress"},
            )
            if status is not ToolObservationStatus.SUCCEEDED
            else None,
            execution_metadata=ExecutionMetadata(
                started_at=started_at,
                finished_at=datetime.now(UTC),
                duration_ms=int((perf_counter() - started) * 1000),
                implementation_version="tool-admission-v1",
                components=(
                    ComponentVersion(
                        component="tool:" + request.tool_id, version=request.tool_version
                    ),
                    ComponentVersion(component="admission_policy", version=policy.policy_version),
                ),
                usage=ExecutionUsage(
                    tool_calls=int(newly_reserved), external_requests=external_requests
                ),
            ),
        )
