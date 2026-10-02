from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from evidrug_api.analysis_input.models import TargetMode
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.database import Base
from evidrug_api.execution_contracts.common import ExecutionLimits
from evidrug_api.trajectory.contracts import (
    TrajectoryActionCandidate,
    TrajectoryActionKind,
    TrajectoryEpisodeStatus,
    TrajectoryEvaluation,
    TrajectoryEvaluationVerdict,
    TrajectoryExecutionMode,
    TrajectoryPreference,
    TrajectoryProfile,
    TrajectoryState,
    TrajectoryStep,
)
from evidrug_api.trajectory.repository import (
    TrajectoryNotCollecting,
    TrajectoryPreferenceMismatch,
    TrajectoryRepository,
    TrajectorySequenceMismatch,
)
from evidrug_api.trajectory.tables import (
    TrajectoryEpisodeRecord,
    TrajectoryEvaluationRecord,
    TrajectoryPreferenceRecord,
    TrajectoryStepRecord,
)


@pytest_asyncio.fixture
async def trajectory_database(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trajectory.db'}")
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
            disease_id="MONDO_0007254",
            disease_name="breast cancer",
            target_mode=TargetMode.SPECIFIED,
            target_name="CDK4",
            original_smiles="CCO",
            canonical_smiles="CCO",
        )
        session.add(analysis)
        await session.commit()
        return analysis.id


def profile(*, max_steps: int = 3) -> TrajectoryProfile:
    return TrajectoryProfile(
        profile_id="admet-adaptive-pilot",
        policy_version="trajectory-policy-v1",
        execution_mode=TrajectoryExecutionMode.REPLAY,
        observation_snapshot_version="admet-fixture-v1",
        limits=ExecutionLimits(
            timeout_seconds=60,
            max_tool_calls=2,
            max_recall_depth=1,
            max_total_tokens=4000,
        ),
        max_steps=max_steps,
    )


def stop_step(episode_id: UUID, *, sequence_number: int = 1) -> TrajectoryStep:
    state = TrajectoryState(remaining_tool_calls=2, remaining_recall_depth=1)
    return TrajectoryStep(
        episode_id=episode_id,
        step_id=uuid4(),
        sequence_number=sequence_number,
        agent_name=AnalysisStageName.ADMET,
        state_before=state,
        available_actions=(
            TrajectoryActionCandidate(
                action_id="stop:insufficient_evidence",
                kind=TrajectoryActionKind.STOP,
                reason_code="preserve_evidence_gap",
                objective="현재 근거와 남은 공백을 보존하고 종료한다.",
            ),
            TrajectoryActionCandidate(
                action_id="tool:toxicity_crosscheck",
                kind=TrajectoryActionKind.CALL_TOOL,
                tool_id="toxicity_crosscheck",
                reason_code="resolve_toxicity_conflict",
                objective="독립 독성 예측으로 상충 여부를 확인한다.",
                expected_fields=("dili_risk",),
            ),
        ),
        selected_action_id="stop:insufficient_evidence",
        state_after=state.model_copy(update={"decision": "conditional_go"}),
    )


@pytest.mark.asyncio
async def test_trajectory_lifecycle_preserves_steps_evaluation_and_preference(
    trajectory_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(trajectory_database)
    first_episode, second_episode = uuid4(), uuid4()
    async with trajectory_database() as session:
        repository = TrajectoryRepository(session)
        await repository.start_episode(analysis_id, profile(), episode_id=first_episode)
        await repository.start_episode(analysis_id, profile(), episode_id=second_episode)
        await repository.append_step(stop_step(first_episode))
        await repository.append_step(stop_step(second_episode))
        await repository.finish_episode(first_episode)
        await repository.finish_episode(second_episode)
        await repository.add_evaluation(
            TrajectoryEvaluation(
                evaluation_id=uuid4(),
                episode_id=first_episode,
                evaluator_id="deterministic-validator",
                evaluator_version="v1",
                verdict=TrajectoryEvaluationVerdict.ACCEPTED,
                quality_score=0.9,
                reason_codes=("schema_valid", "budget_respected"),
            )
        )
        await repository.add_preference(
            TrajectoryPreference(
                preference_id=uuid4(),
                preferred_episode_id=first_episode,
                rejected_episode_id=second_episode,
                evaluator_id="pairwise-judge",
                evaluator_version="v1",
                reason_codes=("lower_cost",),
            )
        )

    async with trajectory_database() as session:
        episode = await session.get(TrajectoryEpisodeRecord, first_episode)
        assert episode is not None
        assert episode.status == TrajectoryEpisodeStatus.COMPLETED.value
        assert await session.scalar(select(func.count(TrajectoryStepRecord.step_id))) == 2
        assert (
            await session.scalar(select(func.count(TrajectoryEvaluationRecord.evaluation_id))) == 1
        )
        assert (
            await session.scalar(select(func.count(TrajectoryPreferenceRecord.preference_id))) == 1
        )


@pytest.mark.asyncio
async def test_trajectory_rejects_sequence_gaps_and_terminal_mutation(
    trajectory_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(trajectory_database)
    episode_id = uuid4()
    async with trajectory_database() as session:
        repository = TrajectoryRepository(session)
        await repository.start_episode(analysis_id, profile(), episode_id=episode_id)
        with pytest.raises(TrajectorySequenceMismatch):
            await repository.append_step(stop_step(episode_id, sequence_number=2))
        await repository.append_step(stop_step(episode_id))
        await repository.finish_episode(episode_id)
        with pytest.raises(TrajectoryNotCollecting):
            await repository.append_step(stop_step(episode_id, sequence_number=2))


@pytest.mark.asyncio
async def test_preference_requires_episodes_from_same_analysis(
    trajectory_database: async_sessionmaker[AsyncSession],
) -> None:
    first_analysis = await create_analysis(trajectory_database)
    second_analysis = await create_analysis(trajectory_database)
    first_episode, second_episode = uuid4(), uuid4()
    async with trajectory_database() as session:
        repository = TrajectoryRepository(session)
        await repository.start_episode(first_analysis, profile(), episode_id=first_episode)
        await repository.start_episode(second_analysis, profile(), episode_id=second_episode)
        await repository.finish_episode(first_episode)
        await repository.finish_episode(second_episode)
        with pytest.raises(TrajectoryPreferenceMismatch):
            await repository.add_preference(
                TrajectoryPreference(
                    preference_id=uuid4(),
                    preferred_episode_id=first_episode,
                    rejected_episode_id=second_episode,
                    evaluator_id="judge",
                    evaluator_version="v1",
                    reason_codes=("better_grounding",),
                )
            )


def test_step_requires_selected_action_to_be_available() -> None:
    state = TrajectoryState(remaining_tool_calls=1, remaining_recall_depth=0)
    with pytest.raises(ValidationError, match="selected action"):
        TrajectoryStep(
            episode_id=uuid4(),
            step_id=uuid4(),
            sequence_number=1,
            agent_name=AnalysisStageName.ADMET,
            state_before=state,
            available_actions=(
                TrajectoryActionCandidate(
                    action_id="stop",
                    kind=TrajectoryActionKind.STOP,
                    reason_code="enough_evidence",
                    objective="종료한다.",
                ),
            ),
            selected_action_id="unknown",
            state_after=state,
        )
