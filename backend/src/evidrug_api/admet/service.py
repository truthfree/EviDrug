"""ADMET 실행·정규화·SQL 저장과 완료된 호출의 재전달을 연결한다."""

from uuid import UUID

from sqlalchemy import select

from evidrug_api.admet.adapter import AdmetToolAdapter
from evidrug_api.admet.contracts import AdmetNormalizedOutput, AdmetToolArguments
from evidrug_api.admet.repository import (
    AdmetPersistenceConflict,
    AdmetPersistenceError,
    AdmetRepository,
)
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.tool_execution.repository import ExecutionConflict, ExecutionIdentity
from evidrug_api.tool_execution.runner import ToolExecutionRunner, input_fingerprint


class AdmetExecutionService:
    """성공 결과를 저장한다. 실행 실패 예외와 SQL 장애는 호출자에게 전파한다."""

    def __init__(
        self,
        adapter: AdmetToolAdapter,
        repository: AdmetRepository,
        *,
        timeout_seconds: float = 300,
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
        arguments: AdmetToolArguments,
    ) -> AdmetNormalizedOutput:
        """완료된 동일 호출은 재추론하지 않고 저장된 결과를 반환한다."""
        smiles = await self.repository.session.scalar(
            select(AnalysisRecord.canonical_smiles).where(AnalysisRecord.id == analysis_id)
        )
        if smiles is None:
            raise AdmetPersistenceError("analysis does not exist")
        if smiles != arguments.canonical_smiles:
            raise AdmetPersistenceConflict("ADMET input does not match analysis")
        identity = ExecutionIdentity(
            tool_call_id,
            request_id,
            analysis_id,
            run_id,
            "admet_ai",
            input_fingerprint(
                {"arguments": arguments.model_dump(mode="json"), "tool_version": "1.4.0"}
            ),
        )

        async def save(output: AdmetNormalizedOutput) -> None:
            await self.repository.save(
                tool_call_id=tool_call_id,
                request_id=request_id,
                analysis_id=analysis_id,
                run_id=run_id,
                output=output,
                commit=False,
            )

        async def load() -> AdmetNormalizedOutput | None:
            output = await self.repository.load_output(tool_call_id)
            if output is not None:
                if output.result.canonical_smiles != arguments.canonical_smiles:
                    raise AdmetPersistenceConflict("completed call has different input")
                await save(output)
            return output

        try:
            return await self.runner.execute(
                identity,
                invoke=lambda: self.adapter.execute(arguments),
                load=load,
                save=save,
                outcome_error=lambda output: None,
            )
        except ExecutionConflict as error:
            raise AdmetPersistenceConflict(str(error)) from error
