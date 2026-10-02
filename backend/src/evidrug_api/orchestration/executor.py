"""단계별 Agent 구현을 공통 orchestration executor 계약에 연결한다."""

from collections.abc import Awaitable, Callable, Mapping

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.execution_contracts.agent import AgentInput
from evidrug_api.orchestration.contracts import (
    AgentExecutionResult,
    AgentExecutor,
    AgentExecutorUnavailable,
)


class RoutedAgentExecutor:
    """구성된 단계만 실행하고 나머지는 명시적인 미구성 오류로 남긴다."""

    def __init__(
        self,
        handlers: Mapping[AnalysisStageName, AgentExecutor],
        *,
        close_callbacks: tuple[Callable[[], Awaitable[None]], ...] = (),
    ) -> None:
        self._handlers = dict(handlers)
        self._close_callbacks = close_callbacks

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        handler = self._handlers.get(agent_input.agent_name)
        if handler is None:
            raise AgentExecutorUnavailable(
                f"{agent_input.agent_name.value} executor is not configured"
            )
        return await handler.execute(agent_input)

    async def aclose(self) -> None:
        """한 분석 작업을 위해 만든 외부 HTTP 연결을 정리한다."""
        for close in reversed(self._close_callbacks):
            await close()
