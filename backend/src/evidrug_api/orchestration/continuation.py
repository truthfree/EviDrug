"""명시적 개발 계획에서 저장 Target/ADMET를 재사용하고 DTA/Decision만 실행한다."""

from typing import cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from evidrug_api.admet.agent import AdmetAgentResult
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.config import Settings
from evidrug_api.decision.agent import DecisionAgent, DecisionResult
from evidrug_api.dta.adapter import DtaToolAdapter
from evidrug_api.dta.agent import DtaAgent, DtaAgentResult
from evidrug_api.dta.deeppurpose import (
    CNN_CNN_BINDINGDB_MODEL,
    MPNN_CNN_BINDINGDB_MODEL,
    DeepPurposeProvider,
)
from evidrug_api.execution_contracts.agent import AgentInput, AgentOutput, AgentOutputStatus
from evidrug_api.execution_contracts.common import ComponentVersion, ExecutionMetadata
from evidrug_api.openai_gateway import build_dacon_openai_client
from evidrug_api.orchestration.contracts import AgentExecutionResult, AgentExecutor
from evidrug_api.orchestration.executor import RoutedAgentExecutor
from evidrug_api.orchestration.reasoning import DaconSpecialistReasoner
from evidrug_api.orchestration.replay_contracts import (
    ContinuationReport,
    ContinuationStageReport,
    ReplayError,
)
from evidrug_api.orchestration.replay_store import OutputParser, OutputParsers, ReplayStore
from evidrug_api.orchestration.service import AnalysisOrchestrator
from evidrug_api.orchestration.tables import AgentRunRecord
from evidrug_api.target_hypothesis.contracts import TargetHypothesisResult
from evidrug_api.tool_admission.bindings import dta_binding


def output_parsers() -> OutputParsers:
    """원본 전문 출력을 범용 dict 대신 도메인 계약으로 검증한다."""

    def parser_for(model: type[BaseModel]) -> OutputParser:
        def parse(value: str) -> AgentOutput[BaseModel]:
            return cast(AgentOutput[BaseModel], model.model_validate_json(value))

        return parse

    return {
        name: parser_for(model)
        for name, model in (
            (AnalysisStageName.TARGET_HYPOTHESIS, AgentOutput[TargetHypothesisResult]),
            (AnalysisStageName.ADMET, AgentOutput[AdmetAgentResult]),
            (AnalysisStageName.DTA, AgentOutput[DtaAgentResult]),
            (AnalysisStageName.DECISION, AgentOutput[DecisionResult]),
        )
    }


class ContinuationExecutor:
    def __init__(self, store: ReplayStore, live: AgentExecutor) -> None:
        self.store, self.live = store, live

    async def execute(self, incoming: AgentInput) -> AgentExecutionResult:
        """매 단계 원본을 재검증하고 live Target/ADMET 호출을 경계에서 차단한다."""
        plan = await self.store.validate(incoming.analysis_id)
        if (
            plan.request.mode != "continue_dta_decision"
            or incoming.agent_name not in ReplayStore.stage_names(plan)
            or incoming.case_input != plan.case_input
        ):
            raise ReplayError("replay_input_mismatch")
        # 원본 참조는 확정 계획과 정확히 일치해야 한다. 새 DTA는 loader가 같은 분석으로 검증한다.
        expected = {
            run.run_id: run.output_sha256
            for run in plan.upstream
            if incoming.agent_name == AnalysisStageName.DECISION
            or run.agent_name == AnalysisStageName.TARGET_HYPOTHESIS
        }
        actual = {
            ref.run_id: ref.output.sha256
            for ref in incoming.upstream_outputs
            if ref.agent_name != AnalysisStageName.DTA
        }
        if expected != actual:
            raise ReplayError("replay_input_mismatch")
        return await self.live.execute(incoming)


def build_continuation_executor(store: ReplayStore, settings: Settings) -> RoutedAgentExecutor:
    """Target/ADMET provider를 생성하지 않고 DTA와 Decision만 등록한다."""
    client = build_dacon_openai_client(settings)
    cnn = DeepPurposeProvider(
        (
            "/opt/dta/.venv/bin/python",
            "/opt/dta/runtime.py",
            "--model",
            CNN_CNN_BINDINGDB_MODEL.model_id,
        ),
        CNN_CNN_BINDINGDB_MODEL,
    )
    mpnn = DeepPurposeProvider(
        (
            "/opt/dta/.venv/bin/python",
            "/opt/dta/runtime.py",
            "--model",
            MPNN_CNN_BINDINGDB_MODEL.model_id,
        ),
        MPNN_CNN_BINDINGDB_MODEL,
    )
    return RoutedAgentExecutor(
        {
            AnalysisStageName.DTA: DtaAgent(
                store.factory,
                (
                    dta_binding(DtaToolAdapter(cnn)),
                    dta_binding(DtaToolAdapter(mpnn), tool_id="dta_mpnn_cnn_bindingdb"),
                ),
                DaconSpecialistReasoner(client),
            ),
            AnalysisStageName.DECISION: DecisionAgent(store.factory, client),
        },
        close_callbacks=(client.close, cnn.aclose, mpnn.aclose),
    )


async def run_continuation(
    store: ReplayStore,
    analysis_id: UUID,
    live: AgentExecutor,
    versions: tuple[ComponentVersion, ...],
    *,
    lease_seconds: int = 1200,
) -> ContinuationReport:
    plan = await store.validate(analysis_id)
    if plan.request.mode != "continue_dta_decision" or plan.runtime_versions != versions:
        raise ReplayError("replay_runtime_version_mismatch")
    await AnalysisOrchestrator(
        store.factory,
        ContinuationExecutor(store, live),
        profile=plan.profile,
        lease_seconds=lease_seconds,
    ).run_continuation(analysis_id, store)
    return await continuation_report(store, analysis_id)


async def continuation_report(store: ReplayStore, analysis_id: UUID) -> ContinuationReport:
    plan = await store.load(analysis_id)
    if plan.request.mode != "continue_dta_decision":
        raise ReplayError("replay_requires_continuation_runner")
    async with store.factory() as session:
        analysis = await session.get(AnalysisRecord, analysis_id)
        assert analysis is not None
        rows = list(
            await session.scalars(
                select(AgentRunRecord)
                .where(AgentRunRecord.analysis_id == analysis_id)
                .order_by(AgentRunRecord.started_at)
            )
        )
        return ContinuationReport(
            analysis_id=analysis_id,
            base_analysis_id=plan.request.base_analysis_id,
            status=analysis.status,
            reused_runs=plan.upstream,
            new_runs=tuple(
                ContinuationStageReport(
                    agent_name=row.agent_name,
                    run_id=row.run_id,
                    status="running" if row.status == "running" else AgentOutputStatus(row.status),
                    error_code=row.error_code,
                    new_usage=ExecutionMetadata.model_validate_json(
                        row.execution_metadata_json
                    ).usage
                    if row.execution_metadata_json
                    else None,
                )
                for row in rows
            ),
        )
