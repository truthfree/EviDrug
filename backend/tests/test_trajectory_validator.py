"""저장된 episode의 변조와 상태 전이를 provider 호출 없이 검증한다."""

import hashlib
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_adaptive_selector import selector_input
from test_trajectory import (
    create_analysis,
    stop_step,
)
from test_trajectory import (
    trajectory_database as database_fixture,
)

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.execution_contracts.common import ExecutionLimits, ExecutionUsage
from evidrug_api.trajectory.adaptive_selector import select_next_tool
from evidrug_api.trajectory.comparison_contracts import (
    ComparisonStrategy,
    PolicyComparisonManifest,
)
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
from evidrug_api.trajectory.repository import TrajectoryRepository
from evidrug_api.trajectory.tables import TrajectoryEpisodeRecord, TrajectoryStepRecord
from evidrug_api.trajectory.validator import validate_stored_episode

trajectory_database = database_fixture


def comparison() -> PolicyComparisonManifest:
    return PolicyComparisonManifest(
        analysis_id=uuid4(),
        admet_snapshot_id=uuid4(),
        ctox_snapshot_id=uuid4(),
        policy_version="comparison-v1",
        limits=ExecutionLimits(timeout_seconds=60, max_tool_calls=2, max_recall_depth=1),
    )


def test_comparison_manifest_has_stable_policy_order_and_budget() -> None:
    manifest = PolicyComparisonManifest.model_validate(
        comparison().model_dump()
        | {
            "strategies": (
                ComparisonStrategy.DECISION_RECALL,
                ComparisonStrategy.BASELINE,
                ComparisonStrategy.ALL_TOOLS,
            )
        }
    )
    assert manifest.ordered_strategies() == (
        ComparisonStrategy.BASELINE,
        ComparisonStrategy.ALL_TOOLS,
        ComparisonStrategy.DECISION_RECALL,
    )
    with pytest.raises(ValidationError, match="requires baseline"):
        PolicyComparisonManifest.model_validate(
            comparison().model_dump() | {"strategies": ["baseline", "baseline", "decision_recall"]}
        )
    with pytest.raises(ValidationError, match="comparison budget"):
        PolicyComparisonManifest.model_validate(
            comparison().model_dump()
            | {
                "limits": ExecutionLimits(
                    timeout_seconds=60, max_tool_calls=1, max_recall_depth=1
                ).model_dump()
            }
        )


def live_profile() -> TrajectoryProfile:
    return TrajectoryProfile(
        profile_id="validator-fixture",
        policy_version="trajectory-validator-v1",
        execution_mode=TrajectoryExecutionMode.LIVE,
        limits=ExecutionLimits(timeout_seconds=60, max_tool_calls=2, max_recall_depth=1),
        max_steps=3,
    )


@pytest.mark.asyncio
async def test_validator_accepts_saved_episode_without_writes(
    trajectory_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(trajectory_database)
    episode_id = uuid4()
    async with trajectory_database() as session:
        repository = TrajectoryRepository(session)
        await repository.start_episode(analysis_id, live_profile(), episode_id=episode_id)
        await repository.append_step(stop_step(episode_id))
        await repository.finish_episode(episode_id)
        report = await validate_stored_episode(session, episode_id)
        assert report.verdict == "accepted"
        assert report.issues == ()
        assert not session.dirty and not session.new


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("profile_hash", "profile_hash_mismatch"),
        ("step_hash", "step_hash_mismatch"),
        ("step_sequence", "step_sequence_mismatch"),
        ("budget_increased", "budget_increased"),
        ("missing_source", "source_run_missing"),
    ],
)
async def test_validator_rejects_tampered_or_impossible_steps(
    trajectory_database: async_sessionmaker[AsyncSession], mutation: str, expected: str
) -> None:
    analysis_id = await create_analysis(trajectory_database)
    episode_id = uuid4()
    step = stop_step(episode_id)
    if mutation == "budget_increased":
        step = step.model_copy(
            update={"state_after": step.state_after.model_copy(update={"remaining_tool_calls": 3})}
        )
    if mutation == "missing_source":
        step = step.model_copy(update={"source_run_id": uuid4()})
    async with trajectory_database() as session:
        repository = TrajectoryRepository(session)
        await repository.start_episode(analysis_id, live_profile(), episode_id=episode_id)
        await repository.append_step(step)
        await repository.finish_episode(episode_id)
        episode = await session.get(TrajectoryEpisodeRecord, episode_id)
        row = await session.get(TrajectoryStepRecord, step.step_id)
        assert episode is not None and row is not None
        if mutation == "profile_hash":
            episode.profile_sha256 = "0" * 64
        if mutation == "step_hash":
            row.step_sha256 = "0" * 64
        if mutation == "step_sequence":
            row.sequence_number = 2
        await session.commit()
        report = await validate_stored_episode(session, episode_id)
        assert report.verdict == "rejected"
        assert expected in {issue.reason_code for issue in report.issues}


@pytest.mark.asyncio
async def test_validator_locates_discontinuous_state_and_invalid_trigger(
    trajectory_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(trajectory_database)
    episode_id = uuid4()
    first = stop_step(episode_id)
    second = stop_step(episode_id, sequence_number=2).model_copy(
        update={"triggering_run_id": uuid4()}
    )
    async with trajectory_database() as session:
        repository = TrajectoryRepository(session)
        await repository.start_episode(analysis_id, live_profile(), episode_id=episode_id)
        await repository.append_step(first)
        await repository.append_step(second)
        await repository.finish_episode(episode_id)
        report = await validate_stored_episode(session, episode_id)
        assert {(issue.reason_code, issue.sequence_number) for issue in report.issues} == {
            ("state_discontinuity", 2),
            ("trigger_run_missing", 2),
        }


@pytest.mark.asyncio
async def test_validator_rejects_missing_step_trigger_and_skipped_call_number(
    trajectory_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(trajectory_database)
    episode_id = uuid4()
    step = stop_step(episode_id).model_copy(
        update={"triggering_step_id": uuid4(), "agent_call_number": 2}
    )
    async with trajectory_database() as session:
        repository = TrajectoryRepository(session)
        await repository.start_episode(analysis_id, live_profile(), episode_id=episode_id)
        await repository.append_step(step)
        await repository.finish_episode(episode_id)
        report = await validate_stored_episode(session, episode_id)
    assert {item.reason_code for item in report.issues} == {
        "step_trigger_missing",
        "agent_call_number_invalid",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("choose_stop", [False, True])
async def test_validator_recomputes_stored_adaptive_choice(
    trajectory_database: async_sessionmaker[AsyncSession], choose_stop: bool
) -> None:
    analysis_id = await create_analysis(trajectory_database)
    episode_id = uuid4()
    config = selector_input()
    selection = select_next_tool(config)
    assert selection.selected_action is not None
    payload = config.model_dump_json()
    digest = hashlib.sha256(payload.encode()).hexdigest()
    profile = live_profile().model_copy(
        update={
            "execution_mode": TrajectoryExecutionMode.REPLAY,
            "observation_snapshot_version": "fixture-v1",
            "selector_input_sha256": digest,
        }
    )
    before = TrajectoryState(
        gap_ids=(config.gap.gap_id,), remaining_tool_calls=1, remaining_recall_depth=1
    )
    stop = TrajectoryActionCandidate(
        action_id="stop:comparison",
        kind=TrajectoryActionKind.STOP,
        reason_code="policy_complete",
        objective="근거를 보존한다.",
    )
    step = TrajectoryStep(
        episode_id=episode_id,
        step_id=uuid4(),
        sequence_number=1,
        agent_name=AnalysisStageName.ADMET,
        state_before=before,
        available_actions=(selection.selected_action, stop),
        excluded_capabilities=tuple(
            TrajectoryCapabilityExclusion(
                capability_id=item.capability_id,
                tool_id=item.tool_id,
                endpoint_id=item.endpoint_id,
                reason_code=item.reason_code,
            )
            for item in selection.candidates
            if not item.selected
        ),
        selected_action_id="stop:comparison"
        if choose_stop
        else selection.selected_action.action_id,
        selector_input_json=payload,
        selector_input_sha256=digest,
        state_after=before
        if choose_stop
        else before.model_copy(update={"remaining_tool_calls": 0}),
    )
    async with trajectory_database() as session:
        repository = TrajectoryRepository(session)
        await repository.start_episode(analysis_id, profile, episode_id=episode_id)
        await repository.append_step(step)
        await repository.finish_episode(episode_id)
        report = await validate_stored_episode(session, episode_id)
    assert report.verdict == ("rejected" if choose_stop else "accepted")
    assert (
        "selector_choice_mismatch" in {item.reason_code for item in report.issues}
    ) == choose_stop


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "tool_calls", "remaining_calls", "expected"),
    [
        (TrajectoryExecutionMode.LIVE, 2, 1, "usage_action_mismatch"),
        (TrajectoryExecutionMode.REPLAY, 1, 2, "replay_tool_invocation_detected"),
    ],
)
async def test_validator_rejects_unaccounted_or_live_replay_tool_calls(
    trajectory_database: async_sessionmaker[AsyncSession],
    mode: TrajectoryExecutionMode,
    tool_calls: int,
    remaining_calls: int,
    expected: str,
) -> None:
    analysis_id = await create_analysis(trajectory_database)
    episode_id = uuid4()
    profile = live_profile().model_copy(
        update={
            "execution_mode": mode,
            "observation_snapshot_version": "fixture-v1"
            if mode is TrajectoryExecutionMode.REPLAY
            else None,
        }
    )
    before = TrajectoryState(remaining_tool_calls=2, remaining_recall_depth=1)
    step = TrajectoryStep(
        episode_id=episode_id,
        step_id=uuid4(),
        sequence_number=1,
        agent_name=AnalysisStageName.ADMET,
        state_before=before,
        available_actions=(
            TrajectoryActionCandidate(
                action_id="tool:ctoxpred2",
                kind=TrajectoryActionKind.CALL_TOOL,
                tool_id="ctoxpred2",
                reason_code="capability_match",
                objective="심장 이온채널 근거를 확인한다.",
            ),
        ),
        selected_action_id="tool:ctoxpred2",
        state_after=before.model_copy(update={"remaining_tool_calls": remaining_calls}),
        usage=ExecutionUsage(tool_calls=tool_calls),
    )
    async with trajectory_database() as session:
        repository = TrajectoryRepository(session)
        await repository.start_episode(analysis_id, profile, episode_id=episode_id)
        await repository.append_step(step)
        await repository.finish_episode(episode_id)
        report = await validate_stored_episode(session, episode_id)
        assert expected in {issue.reason_code for issue in report.issues}


@pytest.mark.asyncio
async def test_validator_rejects_missing_live_tool_ledger(
    trajectory_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(trajectory_database)
    episode_id = uuid4()
    state = TrajectoryState(remaining_tool_calls=2, remaining_recall_depth=1)
    step = TrajectoryStep(
        episode_id=episode_id,
        step_id=uuid4(),
        sequence_number=1,
        agent_name=AnalysisStageName.ADMET,
        state_before=state,
        available_actions=(
            TrajectoryActionCandidate(
                action_id="tool:ctoxpred2",
                kind=TrajectoryActionKind.CALL_TOOL,
                tool_id="ctoxpred2",
                reason_code="capability_match",
                objective="심장 이온채널 근거를 확인한다.",
            ),
        ),
        selected_action_id="tool:ctoxpred2",
        observations=(TrajectoryObservationReference(outcome="succeeded", tool_call_id=uuid4()),),
        state_after=state.model_copy(update={"remaining_tool_calls": 1}),
        usage=ExecutionUsage(tool_calls=1),
    )
    async with trajectory_database() as session:
        repository = TrajectoryRepository(session)
        await repository.start_episode(analysis_id, live_profile(), episode_id=episode_id)
        await repository.append_step(step)
        await repository.finish_episode(episode_id)
        report = await validate_stored_episode(session, episode_id)
        assert ("tool_observation_missing", 1) in {
            (issue.reason_code, issue.sequence_number) for issue in report.issues
        }
