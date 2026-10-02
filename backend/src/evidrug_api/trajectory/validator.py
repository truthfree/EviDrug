"""저장된 trajectory의 구조와 상태 전이를 외부 실행 없이 재검증한다."""

import hashlib
from typing import Literal
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.execution_contracts.common import ContractModel
from evidrug_api.orchestration.tables import AgentRunRecord
from evidrug_api.tool_admission.tables import ToolAdmissionRecord, ToolRunRecord
from evidrug_api.tool_execution.tables import ToolExecutionRecord
from evidrug_api.trajectory.adaptive_selector import AdaptiveSelectorInput, select_next_tool
from evidrug_api.trajectory.contracts import (
    TrajectoryActionKind,
    TrajectoryCapabilityExclusion,
    TrajectoryEpisodeStatus,
    TrajectoryExecutionMode,
    TrajectoryProfile,
    TrajectoryStep,
)
from evidrug_api.trajectory.tables import TrajectoryEpisodeRecord, TrajectoryStepRecord

ValidationReason = Literal[
    "episode_missing",
    "episode_not_terminal",
    "profile_hash_mismatch",
    "profile_schema_invalid",
    "steps_missing",
    "step_sequence_mismatch",
    "step_hash_mismatch",
    "step_schema_invalid",
    "step_identity_mismatch",
    "step_count_exceeded",
    "state_discontinuity",
    "initial_budget_exceeded",
    "budget_increased",
    "recall_budget_transition_invalid",
    "stop_budget_transition_invalid",
    "tool_call_budget_transition_invalid",
    "usage_action_mismatch",
    "replay_tool_invocation_detected",
    "source_run_missing",
    "trigger_run_missing",
    "trigger_not_preceding",
    "step_trigger_missing",
    "step_trigger_invalid",
    "agent_call_number_invalid",
    "observation_run_missing",
    "tool_observation_missing",
    "tool_observation_mismatch",
    "observation_mode_mismatch",
    "selector_profile_mismatch",
    "selector_input_missing",
    "selector_input_hash_mismatch",
    "selector_input_invalid",
    "selector_choice_mismatch",
    "selector_candidates_mismatch",
    "selector_gap_mismatch",
]


class TrajectoryValidationIssue(ContractModel):
    """원문이나 모델 출력을 복사하지 않는 고정 진단 코드."""

    reason_code: ValidationReason
    sequence_number: int | None = Field(default=None, ge=1)


class TrajectoryValidationReport(ContractModel):
    """검증 대상과 첫 실패 위치를 포함하는 결정적 보고서."""

    validator_version: Literal["trajectory-structure-v1"] = "trajectory-structure-v1"
    episode_id: UUID
    verdict: Literal["accepted", "rejected"]
    issues: tuple[TrajectoryValidationIssue, ...] = ()


def _issue(
    reason_code: ValidationReason,
    sequence_number: int | None = None,
) -> TrajectoryValidationIssue:
    return TrajectoryValidationIssue(reason_code=reason_code, sequence_number=sequence_number)


async def validate_stored_episode(
    session: AsyncSession, episode_id: UUID
) -> TrajectoryValidationReport:
    """DB의 불변 기록을 읽기만 하고 해시·연속성·예산·run 참조를 검사한다."""
    episode = await session.get(TrajectoryEpisodeRecord, episode_id)
    if episode is None:
        return _report(episode_id, (_issue("episode_missing"),))
    issues: list[TrajectoryValidationIssue] = []
    if episode.status == TrajectoryEpisodeStatus.COLLECTING.value:
        issues.append(_issue("episode_not_terminal"))
    if hashlib.sha256(episode.profile_json.encode()).hexdigest() != episode.profile_sha256:
        return _report(episode_id, (*issues, _issue("profile_hash_mismatch")))
    try:
        profile = TrajectoryProfile.model_validate_json(episode.profile_json)
    except ValidationError:
        return _report(episode_id, (*issues, _issue("profile_schema_invalid")))
    rows = tuple(
        await session.scalars(
            select(TrajectoryStepRecord)
            .where(TrajectoryStepRecord.episode_id == episode_id)
            .order_by(TrajectoryStepRecord.sequence_number)
        )
    )
    if not rows:
        return _report(episode_id, (*issues, _issue("steps_missing")))
    if len(rows) > profile.max_steps:
        issues.append(_issue("step_count_exceeded"))
    previous: TrajectoryStep | None = None
    seen_run_ids: set[UUID] = set()
    preceding_steps: dict[UUID, TrajectoryStep] = {}
    last_call_numbers: dict[AnalysisStageName, int] = {}
    selector_steps = 0
    for expected_number, row in enumerate(rows, start=1):
        number = row.sequence_number if row.sequence_number > 0 else expected_number
        if row.sequence_number != expected_number:
            issues.append(_issue("step_sequence_mismatch", number))
        if hashlib.sha256(row.step_json.encode()).hexdigest() != row.step_sha256:
            issues.append(_issue("step_hash_mismatch", number))
            continue
        try:
            step = TrajectoryStep.model_validate_json(row.step_json)
        except ValidationError:
            issues.append(_issue("step_schema_invalid", number))
            continue
        if (
            step.episode_id != episode_id
            or step.step_id != row.step_id
            or step.sequence_number != row.sequence_number
            or step.agent_name.value != row.agent_name
        ):
            issues.append(_issue("step_identity_mismatch", number))
            continue
        if step.selector_input_json is not None:
            selector_steps += 1
            issues.extend(_validate_selector_step(profile, step))
        issues.extend(
            await _validate_step(
                session, episode.analysis_id, profile, step, previous, seen_run_ids
            )
        )
        if step.triggering_step_id is not None:
            trigger = preceding_steps.get(step.triggering_step_id)
            if trigger is None:
                issues.append(_issue("step_trigger_missing", number))
            elif not _valid_step_trigger(trigger, step):
                issues.append(_issue("step_trigger_invalid", number))
        if step.agent_call_number is not None:
            previous_call = last_call_numbers.get(step.agent_name)
            if (previous_call is None and step.agent_call_number != 1) or (
                previous_call is not None
                and step.agent_call_number not in (previous_call, previous_call + 1)
            ):
                issues.append(_issue("agent_call_number_invalid", number))
            last_call_numbers[step.agent_name] = step.agent_call_number
        if step.source_run_id is not None:
            seen_run_ids.add(step.source_run_id)
        preceding_steps[step.step_id] = step
        previous = step
    if profile.selector_input_sha256 is not None and selector_steps != 1:
        issues.append(_issue("selector_input_missing"))
    return _report(episode_id, tuple(issues))


def _validate_selector_step(
    profile: TrajectoryProfile, step: TrajectoryStep
) -> tuple[TrajectoryValidationIssue, ...]:
    number = step.sequence_number
    payload = step.selector_input_json
    if payload is None or step.selector_input_sha256 is None:
        return (_issue("selector_input_missing", number),)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    if digest != step.selector_input_sha256:
        return (_issue("selector_input_hash_mismatch", number),)
    if profile.selector_input_sha256 != digest:
        return (_issue("selector_profile_mismatch", number),)
    try:
        selector_input = AdaptiveSelectorInput.model_validate_json(payload)
    except ValidationError:
        return (_issue("selector_input_invalid", number),)
    selection = select_next_tool(selector_input)
    if (
        selector_input.gap.gap_id not in step.state_before.gap_ids
        or selector_input.remaining_tool_calls != step.state_before.remaining_tool_calls
    ):
        return (_issue("selector_gap_mismatch", number),)
    expected_id = (
        selection.selected_action.action_id
        if selection.selected_action is not None
        else "stop:comparison"
    )
    if step.selected_action_id != expected_id:
        return (_issue("selector_choice_mismatch", number),)
    expected_exclusions = tuple(
        TrajectoryCapabilityExclusion(
            capability_id=item.capability_id,
            tool_id=item.tool_id,
            endpoint_id=item.endpoint_id,
            reason_code=item.reason_code,
        )
        for item in selection.candidates
        if not item.selected
    )
    if step.excluded_capabilities != expected_exclusions:
        return (_issue("selector_candidates_mismatch", number),)
    call_actions = tuple(
        action for action in step.available_actions if action.kind is TrajectoryActionKind.CALL_TOOL
    )
    if call_actions != ((selection.selected_action,) if selection.selected_action else ()):
        return (_issue("selector_candidates_mismatch", number),)
    return ()


def _valid_step_trigger(trigger: TrajectoryStep, step: TrajectoryStep) -> bool:
    selected = next(
        action
        for action in trigger.available_actions
        if action.action_id == trigger.selected_action_id
    )
    if selected.kind is TrajectoryActionKind.REQUEST_FOLLOWUP:
        return selected.target_agent == step.agent_name and step.agent_name != trigger.agent_name
    return (
        selected.kind is TrajectoryActionKind.CALL_TOOL
        and trigger.agent_name is AnalysisStageName.ADMET
        and step.agent_name is AnalysisStageName.DECISION
    )


async def _validate_step(
    session: AsyncSession,
    analysis_id: UUID,
    profile: TrajectoryProfile,
    step: TrajectoryStep,
    previous: TrajectoryStep | None,
    seen_run_ids: set[UUID],
) -> tuple[TrajectoryValidationIssue, ...]:
    number = step.sequence_number
    issues: list[TrajectoryValidationIssue] = []
    before, after = step.state_before, step.state_after
    if previous is None:
        if (
            before.remaining_tool_calls > profile.limits.max_tool_calls
            or before.remaining_recall_depth > profile.limits.max_recall_depth
        ):
            issues.append(_issue("initial_budget_exceeded", number))
    elif before != previous.state_after:
        issues.append(_issue("state_discontinuity", number))
    tool_delta = before.remaining_tool_calls - after.remaining_tool_calls
    recall_delta = before.remaining_recall_depth - after.remaining_recall_depth
    if tool_delta < 0 or recall_delta < 0:
        issues.append(_issue("budget_increased", number))
    selected = next(
        action for action in step.available_actions if action.action_id == step.selected_action_id
    )
    if selected.kind is TrajectoryActionKind.REQUEST_FOLLOWUP and (
        tool_delta != 0 or recall_delta != 1
    ):
        issues.append(_issue("recall_budget_transition_invalid", number))
    elif selected.kind is TrajectoryActionKind.STOP and (tool_delta != 0 or recall_delta != 0):
        issues.append(_issue("stop_budget_transition_invalid", number))
    elif selected.kind is TrajectoryActionKind.CALL_TOOL and (
        tool_delta not in (0, 1) or recall_delta != 0
    ):
        issues.append(_issue("tool_call_budget_transition_invalid", number))
    if selected.kind is TrajectoryActionKind.CALL_TOOL:
        expected_calls = (
            0 if profile.execution_mode is TrajectoryExecutionMode.REPLAY else tool_delta
        )
        if step.usage.tool_calls > 1 or step.usage.tool_calls != expected_calls:
            issues.append(_issue("usage_action_mismatch", number))
        if profile.execution_mode is TrajectoryExecutionMode.REPLAY and (
            step.usage.tool_calls > 0 or step.usage.external_requests > 0
        ):
            issues.append(_issue("replay_tool_invocation_detected", number))
    elif step.usage.tool_calls > 0:
        issues.append(_issue("usage_action_mismatch", number))
    if step.source_run_id is not None and not await _same_analysis_run(
        session, step.source_run_id, analysis_id, step.agent_name
    ):
        issues.append(_issue("source_run_missing", number))
    if step.triggering_run_id is not None:
        if not await _same_analysis_run(session, step.triggering_run_id, analysis_id):
            issues.append(_issue("trigger_run_missing", number))
        elif step.triggering_run_id not in seen_run_ids:
            issues.append(_issue("trigger_not_preceding", number))
    for observation in step.observations:
        if observation.run_id is not None and not await _same_analysis_run(
            session, observation.run_id, analysis_id
        ):
            issues.append(_issue("observation_run_missing", number))
        if (
            observation.tool_call_id is not None
            and profile.execution_mode is TrajectoryExecutionMode.LIVE
        ):
            issues.extend(
                await _validate_live_tool_observation(
                    session,
                    analysis_id,
                    step.source_run_id,
                    selected.tool_id,
                    observation.tool_call_id,
                    observation.outcome,
                    number,
                )
            )
    return tuple(issues)


async def _validate_live_tool_observation(
    session: AsyncSession,
    analysis_id: UUID,
    source_run_id: UUID | None,
    selected_tool_id: str | None,
    tool_call_id: UUID,
    outcome: Literal["succeeded", "failed", "rejected", "replay_miss"],
    number: int,
) -> tuple[TrajectoryValidationIssue, ...]:
    if outcome == "replay_miss":
        return (_issue("observation_mode_mismatch", number),)
    admission = await session.scalar(
        select(ToolAdmissionRecord).where(ToolAdmissionRecord.tool_call_id == tool_call_id)
    )
    if admission is None:
        return (_issue("tool_observation_missing", number),)
    tool_run = await session.get(ToolRunRecord, admission.run_id)
    if (
        tool_run is None
        or tool_run.analysis_id != analysis_id
        or admission.run_id != source_run_id
        or admission.tool_id != selected_tool_id
        or (admission.decision == "rejected") != (outcome == "rejected")
    ):
        return (_issue("tool_observation_mismatch", number),)
    if outcome == "rejected":
        return ()
    execution = await session.get(ToolExecutionRecord, tool_call_id)
    if execution is None:
        return (_issue("tool_observation_missing", number),)
    if (
        execution.analysis_id != analysis_id
        or execution.run_id != source_run_id
        or execution.tool_id != selected_tool_id
        or execution.status != ("succeeded" if outcome == "succeeded" else "failed")
    ):
        return (_issue("tool_observation_mismatch", number),)
    return ()


async def _same_analysis_run(
    session: AsyncSession,
    run_id: UUID,
    analysis_id: UUID,
    agent_name: AnalysisStageName | None = None,
) -> bool:
    row = await session.get(AgentRunRecord, run_id)
    return (
        row is not None
        and row.analysis_id == analysis_id
        and (agent_name is None or row.agent_name == agent_name)
    )


def _report(
    episode_id: UUID, issues: tuple[TrajectoryValidationIssue, ...]
) -> TrajectoryValidationReport:
    return TrajectoryValidationReport(
        episode_id=episode_id,
        verdict="rejected" if issues else "accepted",
        issues=issues,
    )
