"""고정 DAG를 실행하고 분석의 terminal 상태를 결정한다."""

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evidrug_api.analysis_jobs.models import AnalysisStageName, AnalysisStatus
from evidrug_api.decision.agent import DecisionResult
from evidrug_api.decision.recall import DecisionRecallRequest, route_recall
from evidrug_api.execution_contracts.agent import (
    AgentInput,
    AgentOutput,
    AgentOutputStatus,
    RecallFeedback,
    UpstreamOutputReference,
)
from evidrug_api.execution_contracts.common import ArtifactReference, ExecutionUsage
from evidrug_api.orchestration.contracts import (
    AgentExecutionResult,
    AgentExecutor,
    AgentExecutorUnavailable,
    ExecutionProfile,
)
from evidrug_api.orchestration.recall_trajectory import record_recall_trajectory
from evidrug_api.orchestration.replay_contracts import ReplayError
from evidrug_api.orchestration.replay_store import ReplayStore
from evidrug_api.orchestration.repository import AnalysisClaim, OrchestrationRepository
from evidrug_api.orchestration.tables import AgentReplayRecord
from evidrug_api.tool_admission.capabilities import CapabilityMatch

logger = logging.getLogger(__name__)


class AgentOutputMismatch(ValueError):
    """executor가 호출 context와 다른 식별자의 출력을 반환했다."""


@dataclass(frozen=True)
class StageOutcome:
    """DAG 의존성 판단에 필요한 최소 단계 결과."""

    agent_name: AnalysisStageName
    run_id: UUID
    status: AgentOutputStatus
    output: ArtifactReference | None = None
    provides_dta_input: bool = False
    recall_request: DecisionRecallRequest | None = None
    error_code: str | None = None
    selection_matches: tuple[CapabilityMatch, ...] = ()
    selection_tool_call_id: UUID | None = None
    selection_replay_snapshot_id: UUID | None = None
    usage: ExecutionUsage = field(default_factory=ExecutionUsage)


class AnalysisOrchestrator:
    """Agent 과학 판단을 수정하지 않고 실행 순서와 실패 전파만 관리한다."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        executor: AgentExecutor,
        *,
        profile: ExecutionProfile | None = None,
        lease_seconds: int = 1200,
    ) -> None:
        self.session_factory = session_factory
        self.executor = executor
        self.profile = profile or ExecutionProfile()
        self.lease_seconds = lease_seconds

    async def run(self, analysis_id: UUID) -> AnalysisStatus:
        """한 분석의 fixed DAG를 최대 한 번 실행하고 영속 terminal 상태를 반환한다."""
        async with self.session_factory() as session:
            if await session.get(AgentReplayRecord, analysis_id) is not None:
                raise ReplayError("replay_requires_single_agent_runner")
        claim = await self._claim(analysis_id)
        if not claim.acquired:
            return claim.analysis_status

        target_task = asyncio.create_task(
            self._execute_stage(claim, AnalysisStageName.TARGET_HYPOTHESIS)
        )
        admet_task = asyncio.create_task(self._execute_stage(claim, AnalysisStageName.ADMET))
        try:
            target = await target_task
            if target.provides_dta_input and target.output is not None:
                dta = await self._execute_stage(
                    claim,
                    AnalysisStageName.DTA,
                    upstream=(target,),
                )
            else:
                dta = await self._skip_stage(
                    claim,
                    AnalysisStageName.DTA,
                    error_code="skipped_due_to_missing_target",
                    upstream=(target,) if target.output is not None else (),
                )
            admet = await admet_task
        finally:
            unfinished = [task for task in (target_task, admet_task) if not task.done()]
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)

        evidence = tuple(outcome for outcome in (target, admet, dta) if outcome.output is not None)
        if not evidence:
            await self._skip_stage(
                claim,
                AnalysisStageName.DECISION,
                error_code="insufficient_evidence",
            )
            await self._finish(
                claim,
                AnalysisStatus.FAILED,
                error_code="insufficient_evidence",
            )
            return AnalysisStatus.FAILED

        decision = await self._execute_stage(
            claim,
            AnalysisStageName.DECISION,
            upstream=evidence,
        )
        if decision.status not in {
            AgentOutputStatus.COMPLETED,
            AgentOutputStatus.PARTIAL_FAILURE,
        }:
            await self._finish(claim, AnalysisStatus.FAILED, error_code="decision_failed")
            return AnalysisStatus.FAILED

        recall = None
        if decision.recall_request is not None:
            request = decision.recall_request
            if self.profile.stage_limits.max_recall_depth < 1:
                await self._finish(
                    claim, AnalysisStatus.FAILED, error_code="recall_budget_exceeded"
                )
                return AnalysisStatus.FAILED
            route = route_recall(request, current_depth=0)
            if admet.output is None:
                await self._finish(
                    claim, AnalysisStatus.FAILED, error_code="recall_baseline_unavailable"
                )
                return AnalysisStatus.FAILED
            initial_decision = decision
            recall = await self._execute_stage(
                claim,
                route.target_agent,
                upstream=(admet,),
                parent_run_id=decision.run_id,
                attempt=2,
                recall_request=request,
            )
            feedback = RecallFeedback(
                run_id=recall.run_id,
                request=request,
                status="succeeded" if recall.output is not None else "failed",
                reason_code=None
                if recall.output is not None
                else (recall.error_code or "recall_agent_failed"),
            )
            second_evidence = (*evidence, recall) if recall.output is not None else evidence
            decision = await self._execute_stage(
                claim,
                AnalysisStageName.DECISION,
                upstream=second_evidence,
                parent_run_id=decision.run_id,
                attempt=2,
                recall_feedback=feedback,
            )
            await record_recall_trajectory(
                self.session_factory,
                analysis_id=analysis_id,
                request=request,
                decision_run_id=initial_decision.run_id,
                recall_run_id=recall.run_id,
                final_run_id=decision.run_id,
                decision_usage=initial_decision.usage,
                recall_usage=recall.usage,
                final_usage=decision.usage,
                recall_error_code=recall.error_code,
                selection_matches=recall.selection_matches,
                selection_tool_call_id=recall.selection_tool_call_id,
                selection_replay_snapshot_id=recall.selection_replay_snapshot_id,
                final_error_code=decision.error_code,
                limits=self.profile.stage_limits,
                policy_version=self.profile.policy_version,
            )
            if (
                decision.status
                not in {
                    AgentOutputStatus.COMPLETED,
                    AgentOutputStatus.PARTIAL_FAILURE,
                }
                or decision.recall_request is not None
            ):
                await self._finish(claim, AnalysisStatus.FAILED, error_code="decision_failed")
                return AnalysisStatus.FAILED

        degraded = any(
            outcome.status is not AgentOutputStatus.COMPLETED
            for outcome in (target, admet, dta, decision, *((recall,) if recall else ()))
        )
        final_status = AnalysisStatus.PARTIAL_FAILURE if degraded else AnalysisStatus.COMPLETED
        await self._finish(claim, final_status)
        return final_status

    async def run_single(self, analysis_id: UUID, store: ReplayStore) -> AnalysisStatus:
        """저장된 단독 실행 계획만 수행하며 기존 claim·timeout·lease·저장을 재사용한다."""
        manifest = await store.validate(analysis_id)
        if manifest.request.mode == "continue_dta_decision":
            raise ReplayError("replay_requires_continuation_runner")
        if self.profile != manifest.profile:
            raise ReplayError("replay_profile_mismatch")
        claim = await self._claim(analysis_id)
        if not claim.acquired:
            return claim.analysis_status
        upstream = tuple(
            StageOutcome(
                agent_name=run.agent_name,
                run_id=run.run_id,
                status=AgentOutputStatus.COMPLETED,
                output=ArtifactReference(
                    artifact_id=run.run_id,
                    schema_name="agent_output",
                    schema_version="1",
                    media_type="application/json",
                    sha256=run.output_sha256,
                ),
            )
            for run in manifest.upstream
            if run.output_sha256 is not None
        )
        try:
            outcome = await self._execute_stage(
                claim,
                manifest.request.agent_name,
                upstream=upstream,
                parent_run_id=manifest.request.source_run_id,
            )
        except asyncio.CancelledError:
            await self._finish(claim, AnalysisStatus.FAILED, error_code="cancelled")
            raise
        status = {
            AgentOutputStatus.COMPLETED: AnalysisStatus.COMPLETED,
            AgentOutputStatus.PARTIAL_FAILURE: AnalysisStatus.PARTIAL_FAILURE,
            AgentOutputStatus.FAILED: AnalysisStatus.FAILED,
            AgentOutputStatus.SKIPPED: AnalysisStatus.FAILED,
        }[outcome.status]
        await self._finish(
            claim,
            status,
            error_code="selected_agent_failed" if status is AnalysisStatus.FAILED else None,
        )
        return status

    async def run_continuation(self, analysis_id: UUID, store: ReplayStore) -> AnalysisStatus:
        """원본 Target/ADMET를 참조해 DTA→Decision만 기존 실행 lifecycle로 수행한다."""
        manifest = await store.validate(analysis_id)
        if manifest.request.mode != "continue_dta_decision" or self.profile != manifest.profile:
            raise ReplayError("replay_profile_mismatch")
        claim = await self._claim(analysis_id)
        if not claim.acquired:
            return claim.analysis_status
        upstream = tuple(
            StageOutcome(
                agent_name=run.agent_name,
                run_id=run.run_id,
                status=AgentOutputStatus.COMPLETED,
                output=ArtifactReference(
                    artifact_id=run.run_id,
                    schema_name="agent_output",
                    schema_version="1",
                    media_type="application/json",
                    sha256=run.output_sha256,
                ),
            )
            for run in manifest.upstream
            if run.output_sha256 is not None
        )
        try:
            dta = await self._execute_stage(
                claim,
                AnalysisStageName.DTA,
                upstream=tuple(
                    item
                    for item in upstream
                    if item.agent_name == AnalysisStageName.TARGET_HYPOTHESIS
                ),
            )
            if dta.output is None:
                await self._skip_stage(
                    claim,
                    AnalysisStageName.DECISION,
                    error_code="skipped_due_to_failed_dta",
                    upstream=upstream,
                )
                await self._finish(claim, AnalysisStatus.FAILED, error_code="dta_failed")
                return AnalysisStatus.FAILED
            decision = await self._execute_stage(
                claim,
                AnalysisStageName.DECISION,
                upstream=(*upstream, dta),
                parent_run_id=manifest.source.run_id,
            )
            if decision.status not in {
                AgentOutputStatus.COMPLETED,
                AgentOutputStatus.PARTIAL_FAILURE,
            }:
                await self._finish(claim, AnalysisStatus.FAILED, error_code="decision_failed")
                return AnalysisStatus.FAILED
            status = (
                AnalysisStatus.COMPLETED
                if dta.status == decision.status == AgentOutputStatus.COMPLETED
                else AnalysisStatus.PARTIAL_FAILURE
            )
            await self._finish(claim, status)
            return status
        except asyncio.CancelledError:
            await self._finish(claim, AnalysisStatus.FAILED, error_code="cancelled")
            raise

    async def _claim(self, analysis_id: UUID) -> AnalysisClaim:
        async with self.session_factory() as session:
            return await OrchestrationRepository(session).claim(
                analysis_id,
                self.profile,
                lease_seconds=self.lease_seconds,
            )

    async def _execute_stage(
        self,
        claim: AnalysisClaim,
        agent_name: AnalysisStageName,
        *,
        upstream: tuple[StageOutcome, ...] = (),
        parent_run_id: UUID | None = None,
        attempt: int = 1,
        recall_request: DecisionRecallRequest | None = None,
        recall_feedback: RecallFeedback | None = None,
    ) -> StageOutcome:
        run_id = uuid4()
        agent_input = self._agent_input(
            claim,
            run_id,
            agent_name,
            upstream,
            attempt=attempt,
            recall_request=recall_request,
            recall_feedback=recall_feedback,
        )
        await self._start_stage(claim, agent_input, parent_run_id=parent_run_id)
        try:
            async with asyncio.timeout(self.profile.stage_limits.timeout_seconds):
                execution = await self.executor.execute(agent_input)
            self._validate_execution(agent_input, execution)
        except asyncio.CancelledError:
            await self._fail_stage(claim, run_id, agent_name, "cancelled")
            raise
        except TimeoutError:
            await self._fail_stage(claim, run_id, agent_name, "agent_timeout")
            return StageOutcome(
                agent_name, run_id, AgentOutputStatus.FAILED, error_code="agent_timeout"
            )
        except AgentExecutorUnavailable:
            await self._fail_stage(claim, run_id, agent_name, "agent_not_configured")
            return StageOutcome(
                agent_name, run_id, AgentOutputStatus.FAILED, error_code="agent_not_configured"
            )
        except ReplayError as error:
            await self._fail_stage(claim, run_id, agent_name, error.code)
            return StageOutcome(agent_name, run_id, AgentOutputStatus.FAILED, error_code=error.code)
        except AgentOutputMismatch:
            await self._fail_stage(claim, run_id, agent_name, "invalid_agent_output")
            return StageOutcome(
                agent_name, run_id, AgentOutputStatus.FAILED, error_code="invalid_agent_output"
            )
        except Exception:
            logger.exception(
                "agent execution failed",
                extra={"analysis_id": str(agent_input.analysis_id), "stage": agent_name.value},
            )
            await self._fail_stage(claim, run_id, agent_name, "agent_execution_failed")
            return StageOutcome(
                agent_name, run_id, AgentOutputStatus.FAILED, error_code="agent_execution_failed"
            )

        artifact = await self._finish_stage(claim, execution.output)
        return StageOutcome(
            agent_name=agent_name,
            run_id=run_id,
            status=execution.output.status,
            output=artifact,
            provides_dta_input=(
                execution.provides_dta_input
                and execution.output.status
                in {AgentOutputStatus.COMPLETED, AgentOutputStatus.PARTIAL_FAILURE}
            ),
            recall_request=(
                execution.output.result.assessment.recall_request
                if agent_name is AnalysisStageName.DECISION
                and isinstance(execution.output.result, DecisionResult)
                else None
            ),
            error_code=execution.output.error.code if execution.output.error else None,
            selection_matches=execution.selection_matches,
            selection_tool_call_id=execution.selection_tool_call_id,
            selection_replay_snapshot_id=execution.selection_replay_snapshot_id,
            usage=execution.output.execution_metadata.usage,
        )

    async def _skip_stage(
        self,
        claim: AnalysisClaim,
        agent_name: AnalysisStageName,
        *,
        error_code: str,
        upstream: tuple[StageOutcome, ...] = (),
    ) -> StageOutcome:
        run_id = uuid4()
        agent_input = self._agent_input(claim, run_id, agent_name, upstream)
        await self._start_stage(claim, agent_input)
        async with self.session_factory() as session:
            await OrchestrationRepository(session).fail_stage(
                analysis_id=agent_input.analysis_id,
                owner_token=claim.owner_token,
                run_id=run_id,
                agent_name=agent_name,
                error_code=error_code,
                policy_version=self.profile.policy_version,
                skipped=True,
            )
        return StageOutcome(agent_name, run_id, AgentOutputStatus.SKIPPED)

    async def _start_stage(
        self, claim: AnalysisClaim, agent_input: AgentInput, *, parent_run_id: UUID | None = None
    ) -> None:
        serialized = agent_input.model_dump_json()
        async with self.session_factory() as session:
            await OrchestrationRepository(session).start_stage(
                analysis_id=agent_input.analysis_id,
                owner_token=claim.owner_token,
                run_id=agent_input.run_id,
                agent_name=agent_input.agent_name,
                input_sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                input_json=serialized,
                parent_run_id=parent_run_id,
                attempt=agent_input.attempt,
                policy_version=self.profile.policy_version,
            )

    async def _finish_stage(
        self, claim: AnalysisClaim, output: AgentOutput[BaseModel]
    ) -> ArtifactReference | None:
        async with self.session_factory() as session:
            return await OrchestrationRepository(session).finish_stage(
                analysis_id=output.analysis_id,
                owner_token=claim.owner_token,
                output=output,
                policy_version=self.profile.policy_version,
            )

    async def _fail_stage(
        self,
        claim: AnalysisClaim,
        run_id: UUID,
        agent_name: AnalysisStageName,
        error_code: str,
    ) -> None:
        async with self.session_factory() as session:
            await OrchestrationRepository(session).fail_stage(
                analysis_id=claim.analysis_id,
                owner_token=claim.owner_token,
                run_id=run_id,
                agent_name=agent_name,
                error_code=error_code,
                policy_version=self.profile.policy_version,
            )

    async def _finish(
        self,
        claim: AnalysisClaim,
        status: AnalysisStatus,
        *,
        error_code: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            await OrchestrationRepository(session).finish_analysis(
                analysis_id=claim.analysis_id,
                owner_token=claim.owner_token,
                status=status,
                error_code=error_code,
                policy_version=self.profile.policy_version,
            )

    def _agent_input(
        self,
        claim: AnalysisClaim,
        run_id: UUID,
        agent_name: AnalysisStageName,
        upstream: tuple[StageOutcome, ...],
        *,
        attempt: int = 1,
        recall_request: DecisionRecallRequest | None = None,
        recall_feedback: RecallFeedback | None = None,
    ) -> AgentInput:
        limits = self.profile.stage_limits
        if attempt == 2:
            limits = limits.model_copy(
                update={
                    "max_recall_depth": 0,
                    "max_tool_calls": min(limits.max_tool_calls, 1)
                    if recall_request is not None
                    else limits.max_tool_calls,
                }
            )
        return AgentInput(
            analysis_id=claim.analysis_id,
            run_id=run_id,
            agent_name=agent_name,
            attempt=attempt,
            case_input=claim.case_input,
            upstream_outputs=tuple(
                UpstreamOutputReference(
                    run_id=outcome.run_id,
                    agent_name=outcome.agent_name,
                    output=outcome.output,
                )
                for outcome in upstream
                if outcome.output is not None
            ),
            execution_limits=limits,
            recall_request=recall_request,
            recall_feedback=recall_feedback,
        )

    @staticmethod
    def _validate_execution(agent_input: AgentInput, execution: AgentExecutionResult) -> None:
        output = execution.output
        if (
            output.analysis_id != agent_input.analysis_id
            or output.run_id != agent_input.run_id
            or output.agent_name is not agent_input.agent_name
        ):
            raise AgentOutputMismatch("agent output identifiers do not match the invocation")
