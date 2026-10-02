"""고정 도구 선택도 Agent별 admission과 공통 실행 ledger를 통과시킨다."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evidrug_api.execution_contracts.agent import AgentInput
from evidrug_api.execution_contracts.tool import ToolObservation, ToolRequest
from evidrug_api.tool_admission.contracts import RunContext, ToolPin, ToolRunPolicy
from evidrug_api.tool_admission.executor import AdmittedToolExecutor
from evidrug_api.tool_admission.registry import ToolBinding, ToolRegistry


class FixedToolRun:
    """서버 고정 binding만 허용하며 호출 한도를 Agent 입력보다 늘리지 않는다."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        binding: ToolBinding | tuple[ToolBinding, ...],
        agent_input: AgentInput,
    ) -> None:
        self.factory = factory
        self.bindings = binding if isinstance(binding, tuple) else (binding,)
        if not self.bindings:
            raise ValueError("fixed tool run requires at least one binding")
        self.binding = self.bindings[0]
        self.registry = ToolRegistry(self.bindings)
        self.agent_input = agent_input
        self.context = RunContext(
            analysis_id=agent_input.analysis_id,
            run_id=agent_input.run_id,
            agent=agent_input.agent_name,
        )

    async def start(self) -> None:
        """도구 호출 전에 권한·예산·deadline의 불변 snapshot을 저장한다."""
        limits = self.agent_input.execution_limits
        async with self.factory() as session:
            await AdmittedToolExecutor(self.registry, session).start_run(
                ToolRunPolicy(
                    context=self.context,
                    policy_version="poc-fixed-tools-v1",
                    allowed_tools=tuple(
                        ToolPin(tool_id=binding.tool_id, version=binding.version)
                        for binding in self.bindings
                    ),
                    max_tool_calls=limits.max_tool_calls,
                    deadline=datetime.now(UTC) + timedelta(seconds=limits.timeout_seconds),
                    call_timeout_seconds=limits.timeout_seconds,
                )
            )

    async def execute(
        self,
        key: str,
        arguments: BaseModel,
        objective: str,
        *,
        binding: ToolBinding | None = None,
    ) -> ToolObservation[BaseModel]:
        """run별 결정적인 ID를 사용하며 별도 세션으로 각 호출을 실행한다."""
        selected = binding or self.binding
        if selected not in self.bindings:
            raise ValueError("binding is not part of this fixed tool run")
        call_id: UUID = uuid5(self.context.run_id, "tool:" + key)
        async with self.factory() as session:
            return await AdmittedToolExecutor(self.registry, session).execute(
                self.context,
                tool_call_id=call_id,
                request=ToolRequest[BaseModel](
                    request_id=uuid5(self.context.run_id, "request:" + key),
                    run_id=self.context.run_id,
                    tool_id=selected.tool_id,
                    tool_version=selected.version,
                    arguments=arguments,
                    objective=objective,
                ),
            )
