"""공통 execution runner 아래 CToxPred2 실행과 SQL 저장을 연결한다."""

from uuid import UUID

from evidrug_api.ctoxpred2.adapter import CtoxToolAdapter
from evidrug_api.ctoxpred2.contracts import CTOX_TOOL_VERSION, CtoxToolArguments, CtoxToolResult
from evidrug_api.ctoxpred2.repository import CtoxPersistenceConflict, CtoxRepository
from evidrug_api.tool_execution.repository import ExecutionConflict, ExecutionIdentity
from evidrug_api.tool_execution.runner import ToolExecutionRunner, input_fingerprint


class CtoxExecutionService:
    def __init__(
        self, adapter: CtoxToolAdapter, repository: CtoxRepository, *, timeout_seconds: float = 300
    ) -> None:
        self.adapter = adapter
        self.repository = repository
        self.runner = ToolExecutionRunner(repository.session, timeout_seconds=timeout_seconds)

    async def execute(
        self,
        *,
        tool_call_id: UUID,
        request_id: UUID,
        analysis_id: UUID,
        run_id: UUID,
        arguments: CtoxToolArguments,
    ) -> CtoxToolResult:
        await self.repository.validate_input(analysis_id, arguments.canonical_smiles)
        identity = ExecutionIdentity(
            tool_call_id,
            request_id,
            analysis_id,
            run_id,
            "ctoxpred2",
            input_fingerprint(
                {
                    "arguments": arguments.model_dump(mode="json"),
                    "tool_version": CTOX_TOOL_VERSION,
                }
            ),
        )

        async def save(result: CtoxToolResult) -> None:
            await self.repository.save(
                tool_call_id=tool_call_id,
                request_id=request_id,
                analysis_id=analysis_id,
                run_id=run_id,
                result=result,
                commit=False,
            )

        async def load() -> CtoxToolResult | None:
            result = await self.repository.load(tool_call_id)
            if result is not None:
                if result.canonical_smiles != arguments.canonical_smiles:
                    raise CtoxPersistenceConflict("completed call has different input")
                await save(result)
            return result

        try:
            return await self.runner.execute(
                identity,
                invoke=lambda: self.adapter.execute(arguments),
                load=load,
                save=save,
                outcome_error=lambda result: None,
            )
        except ExecutionConflict as error:
            raise CtoxPersistenceConflict(str(error)) from error
