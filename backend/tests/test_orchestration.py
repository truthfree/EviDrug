import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from evidrug_api.analysis_input.models import TargetMode
from evidrug_api.analysis_jobs.models import (
    AnalysisStageName,
    AnalysisStageStatus,
    AnalysisStatus,
)
from evidrug_api.analysis_jobs.tables import AnalysisRecord, AnalysisStageRecord
from evidrug_api.database import Base
from evidrug_api.decision.agent import DecisionResult
from evidrug_api.decision.agent import LegacyDecisionAssessment as DecisionAssessment
from evidrug_api.decision.recall import EvidenceGapKind
from evidrug_api.execution_contracts.agent import AgentInput, AgentOutput, AgentOutputStatus
from evidrug_api.execution_contracts.common import (
    ExecutionError,
    ExecutionLimits,
    ExecutionMetadata,
    ExecutionUsage,
)
from evidrug_api.execution_contracts.recall import DecisionRecallRequest
from evidrug_api.orchestration.contracts import (
    AgentExecutionResult,
    ExecutionProfile,
    UnavailableAgentExecutor,
)
from evidrug_api.orchestration.repository import (
    OrchestrationInProgress,
    OrchestrationRepository,
)
from evidrug_api.orchestration.service import AnalysisOrchestrator
from evidrug_api.orchestration.tables import (
    AgentRunRecord,
    AnalysisExecutionRecord,
    ExecutionTraceRecord,
)


class FakeAgentResult(BaseModel):
    """실제 과학 결과 없이 downstream artifact 연결만 검증한다."""

    summary: str


@dataclass(frozen=True)
class ScriptedOutcome:
    status: AgentOutputStatus = AgentOutputStatus.COMPLETED
    provides_dta_input: bool = False
    error_code: str = "scripted_failure"


class ScriptedAgentExecutor:
    """단계별 결과와 blocking을 명시해 DAG 순서를 관찰한다."""

    def __init__(self, outcomes: dict[AnalysisStageName, ScriptedOutcome]) -> None:
        self.outcomes = outcomes
        self.started = {name: asyncio.Event() for name in AnalysisStageName}
        self.release = {name: asyncio.Event() for name in AnalysisStageName}
        self.calls: list[AnalysisStageName] = []
        for event in self.release.values():
            event.set()

    def block(self, stage: AnalysisStageName) -> None:
        self.release[stage].clear()

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        stage = agent_input.agent_name
        self.calls.append(stage)
        self.started[stage].set()
        await self.release[stage].wait()
        scripted = self.outcomes.get(stage, ScriptedOutcome())
        started_at = datetime.now(UTC)
        error = None
        result = None
        if scripted.status in {
            AgentOutputStatus.COMPLETED,
            AgentOutputStatus.PARTIAL_FAILURE,
        }:
            result = FakeAgentResult(summary=f"{stage.value} evidence")
        if scripted.status is not AgentOutputStatus.COMPLETED:
            error = ExecutionError(
                code=scripted.error_code,
                message="scripted outcome",
                retryable=False,
            )
        output = AgentOutput[FakeAgentResult](
            analysis_id=agent_input.analysis_id,
            run_id=agent_input.run_id,
            agent_name=stage,
            status=scripted.status,
            result=result,
            error=error,
            execution_metadata=ExecutionMetadata(
                started_at=started_at,
                finished_at=datetime.now(UTC),
                duration_ms=0,
                implementation_version="scripted-agent-v1",
                usage=ExecutionUsage(),
            ),
        )
        return AgentExecutionResult(
            output=cast(AgentOutput[BaseModel], output),
            provides_dta_input=scripted.provides_dta_input,
        )


@pytest_asyncio.fixture
async def orchestration_database(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'orchestration.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def create_analysis(factory: async_sessionmaker[AsyncSession]) -> UUID:
    async with factory() as session:
        analysis = AnalysisRecord(
            session_fingerprint="a" * 64,
            idempotency_key=str(uuid4()),
            disease_id="MONDO_0004975",
            disease_name="Alzheimer disease",
            target_mode=TargetMode.SPECIFIED,
            target_name="BACE1",
            original_smiles="C(C)O",
            canonical_smiles="CCO",
            stages=[
                AnalysisStageRecord(name=name, position=position)
                for position, name in enumerate(AnalysisStageName, start=1)
            ],
        )
        session.add(analysis)
        await session.commit()
        return analysis.id


def successful_outcomes(
    *, target_available: bool = True
) -> dict[AnalysisStageName, ScriptedOutcome]:
    return {
        AnalysisStageName.TARGET_HYPOTHESIS: ScriptedOutcome(provides_dta_input=target_available),
        AnalysisStageName.ADMET: ScriptedOutcome(),
        AnalysisStageName.DTA: ScriptedOutcome(),
        AnalysisStageName.DECISION: ScriptedOutcome(),
    }


async def loaded_analysis(
    factory: async_sessionmaker[AsyncSession], analysis_id: UUID
) -> AnalysisRecord:
    async with factory() as session:
        record = await OrchestrationRepository(session)._analysis(analysis_id)
        assert record is not None
        return record


@pytest.mark.asyncio
async def test_successful_dag_starts_dta_after_target_without_waiting_for_admet(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(orchestration_database)
    executor = ScriptedAgentExecutor(successful_outcomes())
    executor.block(AnalysisStageName.ADMET)
    task = asyncio.create_task(
        AnalysisOrchestrator(orchestration_database, executor).run(analysis_id)
    )
    try:
        await asyncio.wait_for(executor.started[AnalysisStageName.TARGET_HYPOTHESIS].wait(), 2)
        await asyncio.wait_for(executor.started[AnalysisStageName.ADMET].wait(), 2)
        await asyncio.wait_for(executor.started[AnalysisStageName.DTA].wait(), 2)
        assert not executor.started[AnalysisStageName.DECISION].is_set()
    finally:
        executor.release[AnalysisStageName.ADMET].set()
    assert await task is AnalysisStatus.COMPLETED

    analysis = await loaded_analysis(orchestration_database, analysis_id)
    assert analysis.status is AnalysisStatus.COMPLETED
    assert {stage.status for stage in analysis.stages} == {AnalysisStageStatus.COMPLETED}
    assert executor.calls[-1] is AnalysisStageName.DECISION


class RecallScriptedExecutor(ScriptedAgentExecutor):
    """첫 Decision 요청과 두 번째 최종 판단을 run 경계까지 검증한다."""

    def __init__(self, *, fail_recall: bool = False) -> None:
        super().__init__(successful_outcomes())
        self.inputs: list[AgentInput] = []
        self.fail_recall = fail_recall

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        self.inputs.append(agent_input)
        if agent_input.agent_name is AnalysisStageName.ADMET and agent_input.attempt == 2:
            if self.fail_recall:
                self.outcomes[AnalysisStageName.ADMET] = ScriptedOutcome(
                    status=AgentOutputStatus.FAILED,
                    error_code="provider_unavailable",
                )
            return await super().execute(agent_input)
        if agent_input.agent_name is not AnalysisStageName.DECISION:
            return await super().execute(agent_input)
        request = (
            DecisionRecallRequest(
                gap_kind=EvidenceGapKind.CARDIAC_ION_CHANNEL,
                objective="심장 이온통로 근거 보완",
                reason_code="cardiac_evidence_gap",
            )
            if agent_input.attempt == 1
            else None
        )
        now = datetime.now(UTC)
        output = AgentOutput[DecisionResult](
            analysis_id=agent_input.analysis_id,
            run_id=agent_input.run_id,
            agent_name=agent_input.agent_name,
            status=AgentOutputStatus.COMPLETED,
            result=DecisionResult(
                assessment=DecisionAssessment(
                    decision_phase="request_recall" if request else "final",
                    verdict="conditional_go",
                    rationale="추가 근거가 필요합니다.",
                    used_evidence_ids=("admet:context",),
                    conflicts=(),
                    gaps=("심장 이온통로 근거",),
                    recall_request=request,
                ),
                source_run_ids=tuple(ref.run_id for ref in agent_input.upstream_outputs),
                missing_stages=(),
            ),
            execution_metadata=ExecutionMetadata(
                started_at=now,
                finished_at=now,
                duration_ms=0,
                implementation_version="recall-scripted-v1",
            ),
        )
        return AgentExecutionResult(cast(AgentOutput[BaseModel], output))


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_recall", [False, True])
async def test_decision_recall_reenters_admet_and_decision_once(
    orchestration_database: async_sessionmaker[AsyncSession], fail_recall: bool
) -> None:
    analysis_id = await create_analysis(orchestration_database)
    executor = RecallScriptedExecutor(fail_recall=fail_recall)
    profile = ExecutionProfile(
        stage_limits=ExecutionLimits(timeout_seconds=30, max_tool_calls=1, max_recall_depth=1)
    )

    status = await AnalysisOrchestrator(orchestration_database, executor, profile=profile).run(
        analysis_id
    )

    assert status is (AnalysisStatus.PARTIAL_FAILURE if fail_recall else AnalysisStatus.COMPLETED)
    recall = next(
        item
        for item in executor.inputs
        if item.agent_name is AnalysisStageName.ADMET and item.attempt == 2
    )
    second_decision = next(
        item
        for item in executor.inputs
        if item.agent_name is AnalysisStageName.DECISION and item.attempt == 2
    )
    assert recall.recall_request is not None
    assert recall.recall_request.gap_kind is EvidenceGapKind.CARDIAC_ION_CHANNEL
    assert second_decision.recall_feedback is not None
    assert second_decision.recall_feedback.run_id == recall.run_id
    assert second_decision.recall_feedback.status == ("failed" if fail_recall else "succeeded")
    assert recall.execution_limits.max_tool_calls == 1
    assert recall.execution_limits.max_recall_depth == 0
    assert second_decision.execution_limits.max_recall_depth == 0
    async with orchestration_database() as session:
        runs = list(
            await session.scalars(
                select(AgentRunRecord).where(AgentRunRecord.analysis_id == analysis_id)
            )
        )
    assert sorted(item.attempt for item in runs if item.agent_name == AnalysisStageName.ADMET) == [
        1,
        2,
    ]
    assert sorted(
        item.attempt for item in runs if item.agent_name == AnalysisStageName.DECISION
    ) == [1, 2]
    first_decision_run = next(
        item for item in runs if item.agent_name == AnalysisStageName.DECISION and item.attempt == 1
    )
    recall_run = next(item for item in runs if item.run_id == recall.run_id)
    final_run = next(item for item in runs if item.run_id == second_decision.run_id)
    assert recall_run.parent_run_id == first_decision_run.run_id
    assert final_run.parent_run_id == first_decision_run.run_id


@pytest.mark.asyncio
async def test_missing_target_input_skips_dta_and_finishes_partial_failure(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(orchestration_database)
    executor = ScriptedAgentExecutor(successful_outcomes(target_available=False))

    status = await AnalysisOrchestrator(orchestration_database, executor).run(analysis_id)

    assert status is AnalysisStatus.PARTIAL_FAILURE
    assert AnalysisStageName.DTA not in executor.calls
    analysis = await loaded_analysis(orchestration_database, analysis_id)
    stages = {stage.name: stage.status for stage in analysis.stages}
    assert stages[AnalysisStageName.DTA] is AnalysisStageStatus.SKIPPED
    assert stages[AnalysisStageName.DECISION] is AnalysisStageStatus.COMPLETED
    async with orchestration_database() as session:
        dta_run = await session.scalar(
            select(AgentRunRecord).where(AgentRunRecord.agent_name == AnalysisStageName.DTA)
        )
        assert dta_run is not None
        assert dta_run.error_code == "skipped_due_to_missing_target"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_stage",
    [
        AnalysisStageName.TARGET_HYPOTHESIS,
        AnalysisStageName.ADMET,
        AnalysisStageName.DTA,
    ],
)
async def test_specialist_failure_preserves_evidence_and_runs_decision(
    orchestration_database: async_sessionmaker[AsyncSession],
    failed_stage: AnalysisStageName,
) -> None:
    analysis_id = await create_analysis(orchestration_database)
    outcomes = successful_outcomes()
    outcomes[failed_stage] = ScriptedOutcome(status=AgentOutputStatus.FAILED)
    executor = ScriptedAgentExecutor(outcomes)

    status = await AnalysisOrchestrator(orchestration_database, executor).run(analysis_id)

    assert status is AnalysisStatus.PARTIAL_FAILURE
    assert AnalysisStageName.DECISION in executor.calls
    analysis = await loaded_analysis(orchestration_database, analysis_id)
    assert analysis.status is AnalysisStatus.PARTIAL_FAILURE


@pytest.mark.asyncio
async def test_no_specialist_evidence_skips_decision_and_fails_analysis(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(orchestration_database)
    executor = ScriptedAgentExecutor(
        {
            AnalysisStageName.TARGET_HYPOTHESIS: ScriptedOutcome(status=AgentOutputStatus.FAILED),
            AnalysisStageName.ADMET: ScriptedOutcome(status=AgentOutputStatus.FAILED),
        }
    )

    status = await AnalysisOrchestrator(orchestration_database, executor).run(analysis_id)

    assert status is AnalysisStatus.FAILED
    assert AnalysisStageName.DTA not in executor.calls
    assert AnalysisStageName.DECISION not in executor.calls
    analysis = await loaded_analysis(orchestration_database, analysis_id)
    stages = {stage.name: stage.status for stage in analysis.stages}
    assert stages[AnalysisStageName.DTA] is AnalysisStageStatus.SKIPPED
    assert stages[AnalysisStageName.DECISION] is AnalysisStageStatus.SKIPPED
    assert analysis.error_code == "insufficient_evidence"


@pytest.mark.asyncio
async def test_unconfigured_runtime_fails_explicitly_without_fabricating_results(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(orchestration_database)

    status = await AnalysisOrchestrator(orchestration_database, UnavailableAgentExecutor()).run(
        analysis_id
    )

    assert status is AnalysisStatus.FAILED
    async with orchestration_database() as session:
        runs = list(
            await session.scalars(
                select(AgentRunRecord).where(AgentRunRecord.analysis_id == analysis_id)
            )
        )
    assert {run.error_code for run in runs} == {
        "agent_not_configured",
        "skipped_due_to_missing_target",
        "insufficient_evidence",
    }
    assert all(run.output_json is None for run in runs)


@pytest.mark.asyncio
async def test_decision_failure_makes_analysis_failed_without_deleting_specialist_outputs(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(orchestration_database)
    outcomes = successful_outcomes()
    outcomes[AnalysisStageName.DECISION] = ScriptedOutcome(status=AgentOutputStatus.FAILED)

    status = await AnalysisOrchestrator(
        orchestration_database, ScriptedAgentExecutor(outcomes)
    ).run(analysis_id)

    assert status is AnalysisStatus.FAILED
    async with orchestration_database() as session:
        runs = list(
            await session.scalars(
                select(AgentRunRecord).where(AgentRunRecord.analysis_id == analysis_id)
            )
        )
        assert len([run for run in runs if run.output_json is not None]) == 4
        assert sum(run.status == "completed" for run in runs) == 3
        completed = next(run for run in runs if run.status == "completed")
        assert json.loads(cast(str, completed.output_json))["result"]["summary"]


@pytest.mark.asyncio
async def test_terminal_redelivery_reuses_status_without_new_agent_runs(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(orchestration_database)
    executor = ScriptedAgentExecutor(successful_outcomes())
    orchestrator = AnalysisOrchestrator(orchestration_database, executor)

    assert await orchestrator.run(analysis_id) is AnalysisStatus.COMPLETED
    first_calls = list(executor.calls)
    assert await orchestrator.run(analysis_id) is AnalysisStatus.COMPLETED
    assert executor.calls == first_calls


@pytest.mark.asyncio
async def test_running_redelivery_is_rejected_without_duplicate_agent_runs(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(orchestration_database)
    executor = ScriptedAgentExecutor(successful_outcomes())
    executor.block(AnalysisStageName.TARGET_HYPOTHESIS)
    executor.block(AnalysisStageName.ADMET)
    orchestrator = AnalysisOrchestrator(orchestration_database, executor)
    task = asyncio.create_task(orchestrator.run(analysis_id))
    try:
        await asyncio.wait_for(executor.started[AnalysisStageName.TARGET_HYPOTHESIS].wait(), 2)
        await asyncio.wait_for(executor.started[AnalysisStageName.ADMET].wait(), 2)
        with pytest.raises(OrchestrationInProgress):
            await orchestrator.run(analysis_id)
    finally:
        executor.release[AnalysisStageName.TARGET_HYPOTHESIS].set()
        executor.release[AnalysisStageName.ADMET].set()
    assert await task is AnalysisStatus.COMPLETED


@pytest.mark.asyncio
async def test_expired_lease_fails_running_and_skips_pending_stages(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(orchestration_database)
    profile = ExecutionProfile()
    async with orchestration_database() as session:
        claim = await OrchestrationRepository(session).claim(analysis_id, profile, lease_seconds=60)
    run_id = uuid4()
    async with orchestration_database() as session:
        await OrchestrationRepository(session).start_stage(
            analysis_id=analysis_id,
            owner_token=claim.owner_token,
            run_id=run_id,
            agent_name=AnalysisStageName.TARGET_HYPOTHESIS,
            input_sha256="a" * 64,
            policy_version=profile.policy_version,
        )
    async with orchestration_database() as session:
        await session.execute(
            update(AnalysisExecutionRecord)
            .where(AnalysisExecutionRecord.analysis_id == analysis_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
        assert await OrchestrationRepository(session).recover_expired() == 1
        assert await OrchestrationRepository(session).recover_expired() == 0

    analysis = await loaded_analysis(orchestration_database, analysis_id)
    assert analysis.status is AnalysisStatus.FAILED
    stages = {stage.name: stage.status for stage in analysis.stages}
    assert stages[AnalysisStageName.TARGET_HYPOTHESIS] is AnalysisStageStatus.FAILED
    assert all(
        status is AnalysisStageStatus.SKIPPED
        for name, status in stages.items()
        if name is not AnalysisStageName.TARGET_HYPOTHESIS
    )
    async with orchestration_database() as session:
        trace_types = list(
            await session.scalars(
                select(ExecutionTraceRecord.event_type).where(
                    ExecutionTraceRecord.analysis_id == analysis_id
                )
            )
        )
        assert "lease_expired" in trace_types
