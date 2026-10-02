"""trajectory를 append-only로 저장하고 episode 수명주기를 관리한다."""

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.trajectory.contracts import (
    TrajectoryEpisodeStatus,
    TrajectoryEvaluation,
    TrajectoryPreference,
    TrajectoryProfile,
    TrajectoryStep,
)
from evidrug_api.trajectory.tables import (
    TrajectoryEpisodeRecord,
    TrajectoryEvaluationRecord,
    TrajectoryPreferenceRecord,
    TrajectoryStepRecord,
)


class TrajectoryNotCollecting(RuntimeError):
    """종료된 episode에는 step을 추가하거나 다시 종료할 수 없다."""


class TrajectorySequenceMismatch(RuntimeError):
    """누락이나 덮어쓰기 없이 1부터 연속된 step만 허용한다."""


class TrajectoryPreferenceMismatch(RuntimeError):
    """서로 다른 분석의 episode는 직접 preference pair로 묶지 않는다."""


def _serialized(
    model: TrajectoryProfile | TrajectoryStep | TrajectoryEvaluation | TrajectoryPreference,
) -> tuple[str, str]:
    payload = model.model_dump_json()
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TrajectoryRepository:
    """한 메서드가 하나의 짧은 transaction으로 불변 기록을 추가한다."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def start_episode(
        self,
        analysis_id: UUID,
        profile: TrajectoryProfile,
        *,
        episode_id: UUID | None = None,
    ) -> UUID:
        """분석과 고정 profile을 연결한 collecting episode를 만든다."""
        identifier = episode_id or uuid4()
        profile_json, profile_sha256 = _serialized(profile)
        self.session.add(
            TrajectoryEpisodeRecord(
                episode_id=identifier,
                analysis_id=analysis_id,
                status=TrajectoryEpisodeStatus.COLLECTING.value,
                profile_json=profile_json,
                profile_sha256=profile_sha256,
                started_at=datetime.now(UTC),
            )
        )
        await self.session.commit()
        return identifier

    async def append_step(self, step: TrajectoryStep) -> None:
        """선택 당시 후보 전체를 포함한 다음 연속 step을 추가한다."""
        episode = await self.session.get(TrajectoryEpisodeRecord, step.episode_id)
        if episode is None or episode.status != TrajectoryEpisodeStatus.COLLECTING.value:
            raise TrajectoryNotCollecting(str(step.episode_id))
        current_count = await self.session.scalar(
            select(func.count(TrajectoryStepRecord.step_id)).where(
                TrajectoryStepRecord.episode_id == step.episode_id
            )
        )
        expected = int(current_count or 0) + 1
        if step.sequence_number != expected:
            raise TrajectorySequenceMismatch(
                f"expected sequence {expected}, received {step.sequence_number}"
            )
        profile = TrajectoryProfile.model_validate_json(episode.profile_json)
        if step.sequence_number > profile.max_steps:
            raise TrajectorySequenceMismatch("trajectory profile max_steps exceeded")
        step_json, step_sha256 = _serialized(step)
        self.session.add(
            TrajectoryStepRecord(
                step_id=step.step_id,
                episode_id=step.episode_id,
                sequence_number=step.sequence_number,
                agent_name=step.agent_name.value,
                step_json=step_json,
                step_sha256=step_sha256,
                created_at=datetime.now(UTC),
            )
        )
        await self.session.commit()

    async def finish_episode(
        self,
        episode_id: UUID,
        *,
        error_code: str | None = None,
    ) -> None:
        """episode를 completed 또는 명시적인 failed 상태로 한 번만 종료한다."""
        episode = await self.session.get(TrajectoryEpisodeRecord, episode_id)
        if episode is None or episode.status != TrajectoryEpisodeStatus.COLLECTING.value:
            raise TrajectoryNotCollecting(str(episode_id))
        episode.status = (
            TrajectoryEpisodeStatus.FAILED.value
            if error_code
            else TrajectoryEpisodeStatus.COMPLETED.value
        )
        episode.error_code = error_code
        episode.finished_at = datetime.now(UTC)
        await self.session.commit()

    async def add_evaluation(self, evaluation: TrajectoryEvaluation) -> None:
        """생성 경로를 변경하지 않고 독립 evaluator의 판정을 덧붙인다."""
        episode = await self.session.get(TrajectoryEpisodeRecord, evaluation.episode_id)
        if episode is None or episode.status == TrajectoryEpisodeStatus.COLLECTING.value:
            raise TrajectoryNotCollecting(str(evaluation.episode_id))
        payload, digest = _serialized(evaluation)
        self.session.add(
            TrajectoryEvaluationRecord(
                evaluation_id=evaluation.evaluation_id,
                episode_id=evaluation.episode_id,
                evaluator_id=evaluation.evaluator_id,
                evaluator_version=evaluation.evaluator_version,
                verdict=evaluation.verdict.value,
                evaluation_json=payload,
                evaluation_sha256=digest,
                created_at=datetime.now(UTC),
            )
        )
        await self.session.commit()

    async def add_preference(self, preference: TrajectoryPreference) -> None:
        """같은 분석의 종료된 두 episode 사이 순위를 보존한다."""
        preferred = await self.session.get(TrajectoryEpisodeRecord, preference.preferred_episode_id)
        rejected = await self.session.get(TrajectoryEpisodeRecord, preference.rejected_episode_id)
        if preferred is None or rejected is None:
            raise TrajectoryNotCollecting("preference episode missing")
        if (
            preferred.status == TrajectoryEpisodeStatus.COLLECTING.value
            or rejected.status == TrajectoryEpisodeStatus.COLLECTING.value
        ):
            raise TrajectoryNotCollecting("preference episodes must be terminal")
        if preferred.analysis_id != rejected.analysis_id:
            raise TrajectoryPreferenceMismatch("preference episodes must share one analysis")
        payload, digest = _serialized(preference)
        self.session.add(
            TrajectoryPreferenceRecord(
                preference_id=preference.preference_id,
                preferred_episode_id=preference.preferred_episode_id,
                rejected_episode_id=preference.rejected_episode_id,
                evaluator_id=preference.evaluator_id,
                evaluator_version=preference.evaluator_version,
                preference_json=payload,
                preference_sha256=digest,
                created_at=datetime.now(UTC),
            )
        )
        await self.session.commit()
