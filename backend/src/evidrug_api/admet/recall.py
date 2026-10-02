"""Decision 근거 요청을 ADMET 도구 선택과 관측으로 바꾸는 전문 Agent 경계."""

from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid5

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evidrug_api.admet.agent import AdmetAgentResult
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.ctoxpred2.contracts import (
    CtoxChannel,
    CtoxChannelPrediction,
    CtoxToolArguments,
    CtoxToolResult,
)
from evidrug_api.execution_contracts.agent import AgentInput, AgentOutput, AgentOutputStatus
from evidrug_api.execution_contracts.common import (
    ComponentVersion,
    ContractModel,
    ExecutionError,
    ExecutionMetadata,
    ExecutionUsage,
)
from evidrug_api.execution_contracts.tool import ToolObservation, ToolRequest
from evidrug_api.orchestration.contracts import AgentExecutionResult
from evidrug_api.orchestration.fixed_tools import FixedToolRun
from evidrug_api.orchestration.upstream import InvalidUpstream, load_upstream
from evidrug_api.tool_admission.capabilities import (
    CapabilityMatch,
    EvidenceGap,
    EvidenceGapKind,
    EvidenceKind,
    capabilities_for_bindings,
    match_capabilities,
    validate_capabilities,
)
from evidrug_api.tool_admission.registry import ToolBinding, ToolRegistry
from evidrug_api.trajectory.ctox_snapshot import CtoxReplayExecutor


class AdmetRecallResult(ContractModel):
    """CToxPred2 원장 참조와 Decision에 필요한 최소 채널 관측."""

    source_tool_call_id: UUID
    tool_call_id: UUID
    tool_version: str
    execution_mode: Literal["live", "replay"] = "live"
    replay_snapshot_id: UUID | None = None
    predictions: tuple[CtoxChannelPrediction, ...]
    limitations: tuple[str, ...]
    capability_matches: tuple[CapabilityMatch, ...]


class AdmetRecallAgent:
    """허용된 binding 중 gap을 해결하는 도구만 기존 admission으로 실행한다."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        baseline_binding: ToolBinding,
        ctox_binding: ToolBinding,
        *,
        replay_snapshot_id: UUID | None = None,
    ) -> None:
        self.factory = factory
        self.baseline_binding = baseline_binding
        self.ctox_binding = ctox_binding
        self.replay_snapshot_id = replay_snapshot_id
        self.bindings = (baseline_binding, ctox_binding)
        self.capabilities = capabilities_for_bindings(self.bindings)
        validate_capabilities(self.capabilities, ToolRegistry(self.bindings))

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        request = agent_input.recall_request
        if (
            agent_input.agent_name is not AnalysisStageName.ADMET
            or agent_input.attempt != 2
            or request is None
            or len(agent_input.upstream_outputs) != 1
            or agent_input.upstream_outputs[0].agent_name is not AnalysisStageName.ADMET
        ):
            raise InvalidUpstream("admet_recall_input_mismatch")
        baseline = await load_upstream(
            self.factory,
            agent_input,
            agent_input.upstream_outputs[0],
            AgentOutput[AdmetAgentResult],
        )
        assert baseline.result is not None
        if baseline.run_id != baseline.result.context.source_run_id:
            raise InvalidUpstream("admet_recall_baseline_mismatch")
        started = datetime.now(UTC)
        matches = self._matches(agent_input)
        selected = [item for item in matches if item.matched]
        result = None
        error = None
        usage = ExecutionUsage()
        tool_call_id = None
        if len(selected) != len(CtoxChannel) or any(
            item.tool_id != self.ctox_binding.tool_id for item in selected
        ):
            error = ExecutionError(
                code="recall_capability_unavailable",
                message="요청한 근거를 해결할 수 있는 허용 도구가 없습니다.",
                retryable=False,
            )
        else:
            arguments = CtoxToolArguments(canonical_smiles=agent_input.case_input.canonical_smiles)
            if self.replay_snapshot_id is None:
                tools = FixedToolRun(self.factory, self.ctox_binding, agent_input)
                await tools.start()
                observed = await tools.execute(
                    "cardiac_ion_channel_evidence", arguments, request.objective
                )
            else:
                tool_call_id = uuid5(agent_input.run_id, "tool:cardiac_ion_channel_evidence")
                replay_request = ToolRequest[CtoxToolArguments](
                    request_id=uuid5(agent_input.run_id, "request:cardiac_ion_channel_evidence"),
                    run_id=agent_input.run_id,
                    tool_id=self.ctox_binding.tool_id,
                    tool_version=self.ctox_binding.version,
                    arguments=arguments,
                    objective=request.objective,
                )
                async with self.factory() as session:
                    observed = cast(
                        ToolObservation[BaseModel],
                        await CtoxReplayExecutor(session).execute(
                            self.replay_snapshot_id,
                            tool_call_id=tool_call_id,
                            request=replay_request,
                            expected_analysis_id=agent_input.analysis_id,
                        ),
                    )
            tool_call_id = observed.tool_call_id
            usage = observed.execution_metadata.usage
            error = observed.error
            if observed.result is not None:
                prediction = CtoxToolResult.model_validate(observed.result.model_dump())
                result = AdmetRecallResult(
                    source_tool_call_id=(
                        observed.raw_result.artifact_id
                        if observed.raw_result is not None
                        else observed.tool_call_id
                    ),
                    tool_call_id=observed.tool_call_id,
                    tool_version=self.ctox_binding.version,
                    execution_mode="replay" if self.replay_snapshot_id else "live",
                    replay_snapshot_id=self.replay_snapshot_id,
                    predictions=prediction.predictions,
                    limitations=prediction.limitations,
                    capability_matches=matches,
                )
        finished = datetime.now(UTC)
        output = AgentOutput[AdmetRecallResult](
            analysis_id=agent_input.analysis_id,
            run_id=agent_input.run_id,
            agent_name=agent_input.agent_name,
            status=AgentOutputStatus.COMPLETED if result is not None else AgentOutputStatus.FAILED,
            result=result,
            error=error,
            execution_metadata=ExecutionMetadata(
                started_at=started,
                finished_at=finished,
                duration_ms=int((finished - started).total_seconds() * 1000),
                implementation_version="admet-recall-v1",
                components=(ComponentVersion(component="capability_matcher", version="1"),),
                usage=usage,
            ),
        )
        return AgentExecutionResult(
            cast(AgentOutput[BaseModel], output),
            selection_matches=matches,
            selection_tool_call_id=tool_call_id,
            selection_replay_snapshot_id=self.replay_snapshot_id,
        )

    def _matches(self, agent_input: AgentInput) -> tuple[CapabilityMatch, ...]:
        request = agent_input.recall_request
        assert request is not None
        all_matches: list[CapabilityMatch] = []
        for channel in CtoxChannel:
            gap = EvidenceGap(
                gap_id=agent_input.run_id,
                kind=EvidenceGapKind.CARDIAC_ION_CHANNEL,
                objective=request.objective,
                target_context="candidate molecule",
                required_evidence_kind=EvidenceKind.MODEL_PREDICTION,
                endpoint_id=channel.value,
                expected_field="class_probability",
                priority=1 if request.priority == "high" else 3,
            )
            all_matches.extend(
                match_capabilities(
                    gap,
                    self.capabilities,
                    agent=AnalysisStageName.ADMET,
                    available_inputs=frozenset({"canonical_smiles"}),
                )
            )
        return tuple(all_matches)
