"""행동 후보, 상태 전이와 평가를 보존하는 trajectory 계약."""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.execution_contracts.common import (
    ArtifactReference,
    ContractModel,
    ExecutionLimits,
    ExecutionUsage,
)


class TrajectoryExecutionMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


class TrajectoryEpisodeStatus(StrEnum):
    COLLECTING = "collecting"
    COMPLETED = "completed"
    FAILED = "failed"


class TrajectoryActionKind(StrEnum):
    CALL_TOOL = "call_tool"
    REQUEST_FOLLOWUP = "request_followup"
    STOP = "stop"


class TrajectoryEvaluationVerdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


class TrajectoryProfile(ContractModel):
    """episode 시작 전에 고정하는 선택 정책과 자원 상한."""

    schema_version: Literal["1"] = "1"
    profile_id: str = Field(min_length=1, max_length=120)
    policy_version: str = Field(min_length=1, max_length=120)
    execution_mode: TrajectoryExecutionMode
    observation_snapshot_version: str | None = Field(default=None, max_length=200)
    selector_input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    limits: ExecutionLimits
    max_steps: int = Field(gt=0, le=100)

    @model_validator(mode="after")
    def require_snapshot_for_replay(self) -> Self:
        if self.execution_mode is TrajectoryExecutionMode.REPLAY and not (
            self.observation_snapshot_version
        ):
            raise ValueError("replay trajectories require an observation snapshot version")
        return self


class TrajectoryState(ContractModel):
    """행동 선택 시점에 Agent가 실제로 알 수 있었던 압축 상태."""

    evidence_ids: tuple[str, ...] = ()
    gap_ids: tuple[UUID, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    decision: str | None = Field(default=None, max_length=80)
    remaining_tool_calls: int = Field(ge=0)
    remaining_recall_depth: int = Field(ge=0)


class TrajectoryActionCandidate(ContractModel):
    """선택 결과뿐 아니라 비교 가능한 당시의 전체 행동 후보."""

    action_id: str = Field(min_length=1, max_length=160)
    kind: TrajectoryActionKind
    reason_code: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=1000)
    expected_fields: tuple[str, ...] = ()
    tool_id: str | None = Field(default=None, max_length=120)
    target_agent: AnalysisStageName | None = None

    @model_validator(mode="after")
    def validate_action_target(self) -> Self:
        if self.kind is TrajectoryActionKind.CALL_TOOL and self.tool_id is None:
            raise ValueError("call_tool actions require tool_id")
        if self.kind is TrajectoryActionKind.REQUEST_FOLLOWUP and self.target_agent is None:
            raise ValueError("request_followup actions require target_agent")
        if self.kind is TrajectoryActionKind.STOP and (
            self.tool_id is not None or self.target_agent is not None
        ):
            raise ValueError("stop actions cannot target a tool or agent")
        return self


class TrajectoryCapabilityExclusion(ContractModel):
    """후보에서 제외된 capability와 재현 가능한 단일 이유."""

    capability_id: str = Field(min_length=1, max_length=160)
    tool_id: str = Field(min_length=1, max_length=120)
    endpoint_id: str | None = Field(default=None, min_length=1, max_length=160)
    reason_code: str = Field(min_length=1, max_length=120)


class TrajectoryObservationReference(ContractModel):
    """step 본문에 원 결과를 복제하지 않는 관측 참조."""

    tool_call_id: UUID | None = None
    run_id: UUID | None = None
    artifact: ArtifactReference | None = None
    outcome: Literal["succeeded", "failed", "rejected", "replay_miss"]
    reason_code: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def require_reference_or_failure(self) -> Self:
        if (
            self.outcome == "succeeded"
            and self.tool_call_id is None
            and self.run_id is None
            and self.artifact is None
        ):
            raise ValueError("successful observations require a stored reference")
        if self.outcome != "succeeded" and self.reason_code is None:
            raise ValueError("non-success observations require reason_code")
        return self


class TrajectoryStep(ContractModel):
    """한 상태에서 가능한 행동, 선택, 관측과 다음 상태를 함께 보존한다."""

    schema_version: Literal["1"] = "1"
    episode_id: UUID
    step_id: UUID
    source_run_id: UUID | None = None
    triggering_run_id: UUID | None = None
    triggering_step_id: UUID | None = None
    sequence_number: int = Field(ge=1)
    agent_name: AnalysisStageName
    agent_call_number: int | None = Field(default=None, ge=1)
    state_before: TrajectoryState
    available_actions: tuple[TrajectoryActionCandidate, ...] = Field(min_length=1)
    excluded_capabilities: tuple[TrajectoryCapabilityExclusion, ...] = ()
    selected_action_id: str = Field(min_length=1, max_length=160)
    selector_input_json: str | None = None
    selector_input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observations: tuple[TrajectoryObservationReference, ...] = ()
    state_after: TrajectoryState
    usage: ExecutionUsage = Field(default_factory=ExecutionUsage)

    @model_validator(mode="after")
    def validate_selected_action(self) -> Self:
        if (self.selector_input_json is None) != (self.selector_input_sha256 is None):
            raise ValueError("selector input JSON and hash must be supplied together")
        action_ids = [action.action_id for action in self.available_actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("available action IDs must be unique")
        if self.selected_action_id not in action_ids:
            raise ValueError("selected action must be present in available actions")
        selected = next(
            action
            for action in self.available_actions
            if action.action_id == self.selected_action_id
        )
        if selected.kind is TrajectoryActionKind.STOP and self.observations:
            raise ValueError("stop actions cannot produce observations")
        return self


class TrajectoryEvaluation(ContractModel):
    """결정적 검사, LLM judge 또는 사람 평가의 독립 결과."""

    schema_version: Literal["1"] = "1"
    evaluation_id: UUID
    episode_id: UUID
    evaluator_id: str = Field(min_length=1, max_length=120)
    evaluator_version: str = Field(min_length=1, max_length=200)
    verdict: TrajectoryEvaluationVerdict
    quality_score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    reason_codes: tuple[str, ...] = ()
    notes: str | None = Field(default=None, max_length=2000)


class TrajectoryPreference(ContractModel):
    """같은 분석에서 생성한 두 episode의 순위 근거."""

    schema_version: Literal["1"] = "1"
    preference_id: UUID
    preferred_episode_id: UUID
    rejected_episode_id: UUID
    evaluator_id: str = Field(min_length=1, max_length=120)
    evaluator_version: str = Field(min_length=1, max_length=200)
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_self_preference(self) -> Self:
        if self.preferred_episode_id == self.rejected_episode_id:
            raise ValueError("an episode cannot be preferred over itself")
        return self
