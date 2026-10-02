"""trajectory episode, step, 평가와 preference SQL 모델."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from evidrug_api.database import Base


class TrajectoryEpisodeRecord(Base):
    __tablename__ = "trajectory_episodes"
    __table_args__ = (
        CheckConstraint(
            "(status = 'collecting' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status = 'completed' AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND error_code IS NOT NULL)",
            name="ck_trajectory_episode_outcome",
        ),
    )

    episode_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    profile_json: Mapped[str] = mapped_column(Text(), nullable=False)
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TrajectoryStepRecord(Base):
    __tablename__ = "trajectory_steps"
    __table_args__ = (
        UniqueConstraint("episode_id", "sequence_number", name="uq_trajectory_step_sequence"),
    )

    step_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    episode_id: Mapped[UUID] = mapped_column(
        ForeignKey("trajectory_episodes.episode_id", ondelete="CASCADE"), index=True
    )
    sequence_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(32), nullable=False)
    step_json: Mapped[str] = mapped_column(Text(), nullable=False)
    step_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TrajectoryEvaluationRecord(Base):
    __tablename__ = "trajectory_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "episode_id", "evaluator_id", "evaluator_version", name="uq_trajectory_evaluator"
        ),
    )

    evaluation_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    episode_id: Mapped[UUID] = mapped_column(
        ForeignKey("trajectory_episodes.episode_id", ondelete="CASCADE"), index=True
    )
    evaluator_id: Mapped[str] = mapped_column(String(120), nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(200), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    evaluation_json: Mapped[str] = mapped_column(Text(), nullable=False)
    evaluation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TrajectoryPreferenceRecord(Base):
    __tablename__ = "trajectory_preferences"
    __table_args__ = (
        UniqueConstraint(
            "preferred_episode_id",
            "rejected_episode_id",
            "evaluator_id",
            "evaluator_version",
            name="uq_trajectory_preference_evaluator",
        ),
    )

    preference_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    preferred_episode_id: Mapped[UUID] = mapped_column(
        ForeignKey("trajectory_episodes.episode_id", ondelete="CASCADE"), index=True
    )
    rejected_episode_id: Mapped[UUID] = mapped_column(
        ForeignKey("trajectory_episodes.episode_id", ondelete="CASCADE"), index=True
    )
    evaluator_id: Mapped[str] = mapped_column(String(120), nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(200), nullable=False)
    preference_json: Mapped[str] = mapped_column(Text(), nullable=False)
    preference_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AdmetObservationSnapshotRecord(Base):
    """원 ADMET tool call을 참조하는 불변 replay manifest."""

    __tablename__ = "admet_observation_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id", "snapshot_version", name="uq_admet_snapshot_analysis_version"
        ),
    )

    snapshot_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    snapshot_version: Mapped[str] = mapped_column(String(200), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text(), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CtoxObservationSnapshotRecord(Base):
    """원 CToxPred2 tool call을 참조하는 불변 replay manifest."""

    __tablename__ = "ctoxpred2_observation_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id", "snapshot_version", name="uq_ctox_snapshot_analysis_version"
        ),
    )

    snapshot_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    snapshot_version: Mapped[str] = mapped_column(String(200), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text(), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
