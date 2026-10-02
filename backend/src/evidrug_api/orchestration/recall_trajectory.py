"""Decision recall의 요청, 도구 선택, 재판단을 append-only trajectory로 보존한다."""

from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.decision.recall import DecisionRecallRequest
from evidrug_api.execution_contracts.common import ExecutionLimits, ExecutionUsage
from evidrug_api.tool_admission.capabilities import CapabilityMatch
from evidrug_api.tool_admission.tables import ToolAdmissionRecord
from evidrug_api.trajectory.contracts import (
    TrajectoryActionCandidate,
    TrajectoryActionKind,
    TrajectoryCapabilityExclusion,
    TrajectoryExecutionMode,
    TrajectoryObservationReference,
    TrajectoryProfile,
    TrajectoryState,
    TrajectoryStep,
)
from evidrug_api.trajectory.ctox_snapshot import CtoxSnapshotError, CtoxSnapshotRepository
from evidrug_api.trajectory.repository import TrajectoryRepository


async def record_recall_trajectory(
    factory: async_sessionmaker[AsyncSession],
    *,
    analysis_id: UUID,
    request: DecisionRecallRequest,
    decision_run_id: UUID,
    recall_run_id: UUID,
    final_run_id: UUID,
    decision_usage: ExecutionUsage,
    recall_usage: ExecutionUsage,
    final_usage: ExecutionUsage,
    recall_error_code: str | None,
    selection_matches: tuple[CapabilityMatch, ...],
    selection_tool_call_id: UUID | None,
    selection_replay_snapshot_id: UUID | None,
    final_error_code: str | None,
    limits: ExecutionLimits,
    policy_version: str,
) -> UUID:
    """선택과 관측 참조만 저장한다. tool 결과와 전체 prompt는 복사하지 않는다."""
    async with factory() as session:
        try:
            snapshot = (
                await CtoxSnapshotRepository(session).load(selection_replay_snapshot_id)
                if selection_replay_snapshot_id is not None
                else None
            )
        except CtoxSnapshotError:
            snapshot = None
        profile = TrajectoryProfile(
            profile_id="decision-admet-recall-v1",
            policy_version=policy_version,
            execution_mode=(
                TrajectoryExecutionMode.REPLAY
                if selection_replay_snapshot_id is not None
                else TrajectoryExecutionMode.LIVE
            ),
            observation_snapshot_version=(
                snapshot.snapshot_version
                if snapshot is not None
                else str(selection_replay_snapshot_id)
                if selection_replay_snapshot_id is not None
                else None
            ),
            limits=limits,
            max_steps=3,
        )
        repository = TrajectoryRepository(session)
        episode_id = await repository.start_episode(analysis_id, profile)
        admission = await session.scalar(
            select(ToolAdmissionRecord).where(ToolAdmissionRecord.run_id == recall_run_id)
        )
        selected = next((item for item in selection_matches if item.matched), None)
        selected_tool_id = (
            admission.tool_id
            if admission
            else (selected.tool_id if selected and selection_tool_call_id else None)
        )
        selected_version = (
            admission.tool_version if admission else (selected.tool_version if selected else None)
        )
        gap_state = TrajectoryState(
            gap_ids=(recall_run_id,),
            remaining_tool_calls=min(1, limits.max_tool_calls),
            remaining_recall_depth=1,
        )
        routed_state = gap_state.model_copy(update={"remaining_recall_depth": 0})
        await repository.append_step(
            TrajectoryStep(
                episode_id=episode_id,
                step_id=uuid4(),
                source_run_id=decision_run_id,
                sequence_number=1,
                agent_name=AnalysisStageName.DECISION,
                state_before=gap_state,
                available_actions=(
                    TrajectoryActionCandidate(
                        action_id="request:admet:cardiac_ion_channels",
                        kind=TrajectoryActionKind.REQUEST_FOLLOWUP,
                        reason_code=request.reason_code,
                        objective=request.objective,
                        target_agent=AnalysisStageName.ADMET,
                    ),
                ),
                selected_action_id="request:admet:cardiac_ion_channels",
                observations=(
                    TrajectoryObservationReference(outcome="succeeded", run_id=decision_run_id),
                ),
                usage=decision_usage,
                state_after=routed_state,
            )
        )
        action = TrajectoryActionCandidate(
            action_id=(
                f"call:{selected_tool_id}:{selected_version}"
                if selected_tool_id is not None
                else "stop:no_capability"
            ),
            kind=(
                TrajectoryActionKind.CALL_TOOL
                if selected_tool_id is not None
                else TrajectoryActionKind.STOP
            ),
            reason_code=(
                "capability_match"
                if selected_tool_id is not None
                else (recall_error_code or "recall_capability_unavailable")
            ),
            objective=request.objective,
            expected_fields=("label", "class_probability") if selected_tool_id else (),
            tool_id=selected_tool_id,
        )
        outcome: Literal["succeeded", "failed", "rejected", "replay_miss"] = (
            "rejected"
            if admission is not None and admission.decision == "rejected"
            else "replay_miss"
            if selection_replay_snapshot_id is not None and recall_error_code
            else "failed"
            if recall_error_code
            else "succeeded"
        )
        observation = (
            (
                TrajectoryObservationReference(
                    outcome=outcome,
                    tool_call_id=admission.tool_call_id if admission else selection_tool_call_id,
                    run_id=recall_run_id if outcome == "succeeded" else None,
                    reason_code=(
                        (admission.reason_code if admission else None) or recall_error_code
                    )
                    if outcome != "succeeded"
                    else None,
                ),
            )
            if selected_tool_id is not None
            else ()
        )
        after_recall = TrajectoryState(
            evidence_ids=(str(selection_tool_call_id),)
            if selection_tool_call_id is not None and outcome == "succeeded"
            else (),
            gap_ids=() if outcome == "succeeded" else (recall_run_id,),
            remaining_tool_calls=gap_state.remaining_tool_calls
            - (1 if admission is not None and admission.decision == "approved" else 0),
            remaining_recall_depth=0,
        )
        await repository.append_step(
            TrajectoryStep(
                episode_id=episode_id,
                step_id=uuid4(),
                source_run_id=recall_run_id,
                triggering_run_id=decision_run_id,
                sequence_number=2,
                agent_name=AnalysisStageName.ADMET,
                state_before=routed_state,
                available_actions=(action,),
                excluded_capabilities=tuple(
                    TrajectoryCapabilityExclusion(
                        capability_id=match.capability_id,
                        tool_id=match.tool_id,
                        endpoint_id=match.endpoint_id,
                        reason_code=match.reason_code.value,
                    )
                    for match in selection_matches
                    if not match.matched
                ),
                selected_action_id=action.action_id,
                observations=observation,
                usage=recall_usage,
                state_after=after_recall,
            )
        )
        await repository.append_step(
            TrajectoryStep(
                episode_id=episode_id,
                step_id=uuid4(),
                source_run_id=final_run_id,
                triggering_run_id=recall_run_id,
                sequence_number=3,
                agent_name=AnalysisStageName.DECISION,
                state_before=after_recall,
                available_actions=(
                    TrajectoryActionCandidate(
                        action_id="stop:final_decision",
                        kind=TrajectoryActionKind.STOP,
                        reason_code="decision_finalized"
                        if final_error_code is None
                        else final_error_code,
                        objective="추가 근거를 반영해 연구 판단을 종료한다.",
                    ),
                ),
                selected_action_id="stop:final_decision",
                usage=final_usage,
                state_after=after_recall,
            )
        )
        await repository.finish_episode(
            episode_id, error_code=final_error_code or recall_error_code
        )
    return episode_id
