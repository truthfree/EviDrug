"""외부 호출을 선택한 Agent 하나로 제한하는 개발 실행 경계."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.dta.agent import DtaAgentResult
from evidrug_api.execution_contracts.agent import AgentInput, AgentWarning
from evidrug_api.execution_contracts.common import (
    ComponentVersion,
    ExecutionMetadata,
    ExecutionUsage,
    TokenUsage,
)
from evidrug_api.orchestration.contracts import AgentExecutionResult, AgentExecutor
from evidrug_api.orchestration.replay_contracts import ReplayError, ReplayReport
from evidrug_api.orchestration.replay_store import ReplayStore
from evidrug_api.orchestration.service import AnalysisOrchestrator
from evidrug_api.orchestration.tables import AgentRunRecord


class ReplayExecutor:
    """매 호출의 출처를 확인하고 live 실행 또는 저장 출력 복사 중 하나만 수행한다."""

    def __init__(self, store: ReplayStore, live_executor: AgentExecutor | None = None) -> None:
        self.store = store
        self.live_executor = live_executor

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        manifest = await self.store.validate(agent_input.analysis_id)
        if (
            manifest.request.agent_name != agent_input.agent_name
            or manifest.case_input != agent_input.case_input
            or {ref.run_id: ref.output.sha256 for ref in agent_input.upstream_outputs}
            != {run.run_id: run.output_sha256 for run in manifest.upstream}
        ):
            raise ReplayError("replay_input_mismatch")
        if manifest.request.mode == "live_agent":
            if self.live_executor is None:
                raise ReplayError("replay_live_executor_missing")
            return await self.live_executor.execute(agent_input)
        if manifest.request.mode != "reuse_output":
            raise ReplayError("replay_evidence_snapshot_missing")
        started = datetime.now(UTC)
        async with self.store.factory() as session:
            source = await session.get(AgentRunRecord, manifest.source.run_id)
            if source is None:
                raise ReplayError("replay_run_missing")
            output = self.store.parse_output(source)
        # 새 규칙으로 결과를 재해석하지 않는다. 원 출처·비용은 manifest에 보존한다.
        copied = output.model_copy(
            update={
                "analysis_id": agent_input.analysis_id,
                "run_id": agent_input.run_id,
                "evidence_claims": tuple(
                    claim.model_copy(update={"producer_run_id": agent_input.run_id})
                    for claim in output.evidence_claims
                ),
                "warnings": (
                    *output.warnings,
                    AgentWarning(
                        code="stored_output_reused",
                        message=f"저장된 run {source.run_id}의 결과이며 새 모델 판단이 아닙니다.",
                    ),
                ),
                "execution_metadata": ExecutionMetadata(
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    duration_ms=max(0, int((datetime.now(UTC) - started).total_seconds() * 1000)),
                    implementation_version="stored-agent-output-v1",
                    components=(ComponentVersion(component="replay", version="output-reuse-v1"),)
                    + (
                        (
                            ComponentVersion(
                                component="dta_assay_policy",
                                version=output.result.assay_policy_version,
                            ),
                        )
                        if isinstance(output.result, DtaAgentResult)
                        else ()
                    ),
                    usage=ExecutionUsage(
                        token_usage=TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0)
                    ),
                ),
            }
        )
        # model_copy는 검증을 생략하므로 원래 typed schema를 한 번 더 적용한다.
        copied = type(output).model_validate_json(copied.model_dump_json())
        return AgentExecutionResult(output=copied)


async def run_replay(
    store: ReplayStore,
    analysis_id: UUID,
    *,
    live_executor: AgentExecutor | None = None,
    runtime_versions: tuple[ComponentVersion, ...] = (),
    lease_seconds: int = 1200,
) -> ReplayReport:
    """확정 계획의 단일 Agent만 실행한다. 같은 ID 재전달은 공통 runner가 차단한다."""
    manifest = await store.validate(analysis_id)
    if manifest.request.mode == "live_agent":
        if manifest.runtime_versions != runtime_versions:
            raise ReplayError("replay_runtime_version_mismatch")
        if live_executor is None:
            raise ReplayError("replay_live_executor_missing")
    runner = AnalysisOrchestrator(
        store.factory,
        ReplayExecutor(store, live_executor),
        profile=manifest.profile,
        lease_seconds=lease_seconds,
    )
    await runner.run_single(analysis_id, store)
    return await replay_report(store, analysis_id)


async def replay_report(store: ReplayStore, analysis_id: UUID) -> ReplayReport:
    """저장 상태를 조회하며 알 수 없는 새 비용을 0으로 표시하지 않는다."""
    manifest = await store.load(analysis_id)
    async with store.factory() as session:
        analysis = await session.get(AnalysisRecord, analysis_id)
        if analysis is None:
            raise ReplayError("replay_analysis_missing")
        run = await session.scalar(
            select(AgentRunRecord).where(AgentRunRecord.analysis_id == analysis_id)
        )
        metadata = (
            ExecutionMetadata.model_validate_json(run.execution_metadata_json)
            if run is not None and run.execution_metadata_json is not None
            else None
        )
        reused = manifest.upstream
        if manifest.request.mode == "reuse_output":
            reused = (manifest.source, *reused)
        return ReplayReport(
            analysis_id=analysis_id,
            status=analysis.status,
            mode=manifest.request.mode,
            agent_name=manifest.request.agent_name,
            base_analysis_id=manifest.request.base_analysis_id,
            source_run_id=manifest.request.source_run_id,
            new_run_id=run.run_id if run else None,
            error_code=run.error_code if run else analysis.error_code,
            new_usage=metadata.usage if metadata else None,
            original_source_usage=manifest.source.metadata.usage
            if manifest.source.metadata
            else None,
            reused_runs=reused,
            uncalled_agents=tuple(
                name
                for name in AnalysisStageName
                if name != manifest.request.agent_name or manifest.request.mode == "reuse_output"
            ),
        )
