"""DTA 결과를 저장하고 완료된 호출의 재전달에서는 추론을 반복하지 않는다."""

from uuid import UUID

from evidrug_api.dta.adapter import DtaToolAdapter
from evidrug_api.dta.contracts import DtaArguments, DtaResult
from evidrug_api.dta.repository import DtaPersistenceConflict, DtaRepository
from evidrug_api.tool_execution.repository import ExecutionConflict, ExecutionIdentity
from evidrug_api.tool_execution.runner import ToolExecutionRunner, input_fingerprint


class DtaExecutionService:
    """공통 ledger 소유권 아래 DTA 실행과 최종 결과를 저장한다."""

    def __init__(
        self,
        adapter: DtaToolAdapter,
        repository: DtaRepository,
        *,
        tool_id: str = "dta",
        timeout_seconds: float = 300,
    ) -> None:
        self.adapter = adapter
        self.repository = repository
        self.tool_id = tool_id
        self.runner = ToolExecutionRunner(repository.session, timeout_seconds=timeout_seconds)

    async def execute(
        self,
        *,
        tool_call_id: UUID,
        request_id: UUID,
        analysis_id: UUID,
        run_id: UUID,
        arguments: DtaArguments,
    ) -> DtaResult:
        """같은 입력·모델·식별자의 재전달은 저장된 결과를 그대로 반환한다."""
        await self.repository.validate_input(analysis_id, arguments.canonical_smiles)
        identity = ExecutionIdentity(
            tool_call_id,
            request_id,
            analysis_id,
            run_id,
            self.tool_id,
            input_fingerprint(
                {
                    "arguments": arguments.model_dump(mode="json"),
                    "model": self.adapter.provider.model.model_dump(mode="json"),
                }
            ),
        )

        async def save(result: DtaResult) -> None:
            await self.repository.save(
                tool_call_id=tool_call_id,
                request_id=request_id,
                analysis_id=analysis_id,
                run_id=run_id,
                result=result,
                commit=False,
            )

        async def load() -> DtaResult | None:
            result = await self.repository.load(tool_call_id)
            if result is not None:
                if result.arguments != arguments or result.model != self.adapter.provider.model:
                    raise DtaPersistenceConflict("completed call has different input or model")
                await save(result)  # ledger 이전 결과도 원래 식별자를 검증한다.
            return result

        try:
            return await self.runner.execute(
                identity,
                invoke=lambda: self.adapter.execute(arguments),
                load=load,
                save=save,
                outcome_error=lambda result: result.error_code,
            )
        except ExecutionConflict as error:
            raise DtaPersistenceConflict(str(error)) from error
