"""한 분석의 고정 ADMET/CToxPred2 관측에서 세 정책의 replay episode를 생성한다."""

import hashlib
import json
from typing import Literal, Self
from uuid import UUID, uuid4, uuid5

from pydantic import Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.admet.contracts import AdmetToolArguments, AdmetToolResult
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.ctoxpred2.contracts import CtoxToolArguments, CtoxToolResult
from evidrug_api.decision.projection import (
    admet_evidence,
    cardiac_evidence,
    decision_payload,
    decision_payload_json,
    decision_payload_sha256,
    dta_evidence,
    target_evidence,
)
from evidrug_api.execution_contracts.common import ContractModel
from evidrug_api.execution_contracts.tool import ToolObservation, ToolRequest
from evidrug_api.tool_admission.capabilities import (
    EvidenceGapKind,
    capabilities_for_bindings,
    validate_capabilities,
)
from evidrug_api.tool_admission.registry import ToolRegistry
from evidrug_api.trajectory.adaptive_selector import (
    AdaptiveSelection,
    AdaptiveSelectorInput,
    select_next_tool,
)
from evidrug_api.trajectory.admet_snapshot import (
    AdmetObservationSnapshot,
    AdmetReplayExecutor,
    AdmetSnapshotRepository,
)
from evidrug_api.trajectory.comparison_contracts import (
    COMPARISON_ORDER,
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
from evidrug_api.trajectory.ctox_snapshot import (
    CtoxObservationSnapshot,
    CtoxReplayExecutor,
    CtoxSnapshotRepository,
)
from evidrug_api.trajectory.decision_sources import (
    DecisionSourceManifest,
    VerifiedDecisionSources,
    verify_decision_sources,
)
from evidrug_api.trajectory.repository import TrajectoryRepository
from evidrug_api.trajectory.validator import validate_stored_episode


class PolicyComparisonError(RuntimeError):
    """입력 snapshot이 없거나 원본과 맞지 않아 비교를 시작할 수 없다."""


class PolicyOutcome(ContractModel):
    strategy: ComparisonStrategy
    episode_id: UUID
    selected_tool_ids: tuple[str, ...]
    policy_excluded_tool_ids: tuple[str, ...] = ()
    attempted_observations: int = Field(ge=0)
    successful_observations: int = Field(ge=0)
    failed_observations: int = Field(ge=0)
    recall_requests: int = Field(ge=0)
    replay_duration_ms: int = Field(ge=0)
    replay_misses: int = Field(ge=0)
    covered_gap_count: int = Field(ge=0)
    unresolved_gap_count: int = Field(ge=0)
    live_tool_calls: Literal[0] = 0
    external_requests: Literal[0] = 0
    replay_token_count: Literal[0] = 0
    live_duration_ms: None = None
    validation_verdict: Literal["accepted", "rejected"]
    validation_reason_codes: tuple[str, ...] = ()
    decision_inputs: tuple["PolicyDecisionInput", ...] = ()

    @model_validator(mode="after")
    def check_denominators(self) -> Self:
        if self.attempted_observations != len(self.selected_tool_ids) or (
            self.successful_observations + self.failed_observations != self.attempted_observations
        ):
            raise ValueError("comparison observation denominator mismatch")
        if self.replay_misses > self.failed_observations:
            raise ValueError("replay misses must be failed observations")
        if self.covered_gap_count + self.unresolved_gap_count != 1:
            raise ValueError("comparison requires exactly one cardiac gap")
        return self


class PolicyDecisionInput(ContractModel):
    """Decision 직전 입력. source manifest가 있으면 공통 전체 projection도 보존한다."""

    projection_version: Literal["comparison-decision-input-v1", "comparison-decision-input-v2"] = (
        "comparison-decision-input-v1"
    )
    call_number: int = Field(ge=1)
    source_step_id: UUID | None = None
    evidence_ids: tuple[str, ...]
    observation_refs: tuple[TrajectoryObservationReference, ...]
    unresolved_gap_ids: tuple[UUID, ...]
    source_run_ids: tuple[UUID, ...] = ()
    full_payload_json: str | None = None
    full_payload_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_full_projection(self) -> Self:
        if (self.projection_version == "comparison-decision-input-v2") != (
            self.full_payload_json is not None
        ):
            raise ValueError("decision input projection version does not match payload")
        if (self.full_payload_json is None) != (self.full_payload_sha256 is None):
            raise ValueError("full decision payload and hash must appear together")
        if self.full_payload_json is not None:
            if len(self.source_run_ids) != 3:
                raise ValueError("full decision payload requires three pinned specialist runs")
            if (
                hashlib.sha256(self.full_payload_json.encode()).hexdigest()
                != self.full_payload_sha256
            ):
                raise ValueError("full decision payload hash mismatch")
        return self


class PolicyComparisonReport(ContractModel):
    schema_version: Literal["1"] = "1"
    analysis_id: UUID
    admet_snapshot_id: UUID
    ctox_snapshot_id: UUID
    policy_version: str
    case_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_sources: DecisionSourceManifest | None = None
    outcomes: tuple[PolicyOutcome, ...]

    @model_validator(mode="after")
    def require_complete_comparison(self) -> Self:
        if tuple(item.strategy for item in self.outcomes) != COMPARISON_ORDER:
            raise ValueError("comparison report must contain all strategies in fixed order")
        if len({item.episode_id for item in self.outcomes}) != len(self.outcomes):
            raise ValueError("comparison report episode identities must be distinct")
        return self


class AdaptiveComparisonReport(ContractModel):
    fixed: PolicyComparisonReport
    adaptive: PolicyOutcome
    selection: AdaptiveSelection

    @model_validator(mode="after")
    def same_case(self) -> Self:
        if self.adaptive.strategy is not ComparisonStrategy.ADAPTIVE:
            raise ValueError("adaptive comparison requires adaptive outcome")
        if self.adaptive.episode_id in {item.episode_id for item in self.fixed.outcomes}:
            raise ValueError("adaptive episode must be distinct from fixed episodes")
        return self


class PolicyAggregate(ContractModel):
    strategy: ComparisonStrategy
    case_count: int = Field(ge=0)
    accepted_episodes: int = Field(ge=0)
    attempted_observations: int = Field(ge=0)
    successful_observations: int = Field(ge=0)
    failed_observations: int = Field(ge=0)
    replay_misses: int = Field(ge=0)
    unresolved_gaps: int = Field(ge=0)
    covered_gaps: int = Field(ge=0)
    policy_excluded_cases: int = Field(ge=0)
    replay_duration_ms: int = Field(ge=0)
    live_duration_ms: None = None
    live_tool_calls: Literal[0] = 0
    external_requests: Literal[0] = 0
    replay_token_count: Literal[0] = 0

    @property
    def failure_rate(self) -> float | None:
        if self.attempted_observations == 0:
            return None
        return self.failed_observations / self.attempted_observations

    @property
    def validator_pass_rate(self) -> float | None:
        if self.case_count == 0:
            return None
        return self.accepted_episodes / self.case_count


def aggregate_reports(reports: tuple[PolicyComparisonReport, ...]) -> tuple[PolicyAggregate, ...]:
    """실패·miss case를 제외하지 않고 정책별 분모를 유지한다."""
    if len({report.analysis_id for report in reports}) != len(reports):
        raise PolicyComparisonError("comparison_duplicate_case")
    return tuple(
        PolicyAggregate(
            strategy=strategy,
            case_count=len(reports),
            accepted_episodes=sum(
                outcome.validation_verdict == "accepted"
                for report in reports
                for outcome in report.outcomes
                if outcome.strategy is strategy
            ),
            attempted_observations=sum(
                outcome.attempted_observations
                for report in reports
                for outcome in report.outcomes
                if outcome.strategy is strategy
            ),
            successful_observations=sum(
                outcome.successful_observations
                for report in reports
                for outcome in report.outcomes
                if outcome.strategy is strategy
            ),
            failed_observations=sum(
                outcome.failed_observations
                for report in reports
                for outcome in report.outcomes
                if outcome.strategy is strategy
            ),
            replay_misses=sum(
                outcome.replay_misses
                for report in reports
                for outcome in report.outcomes
                if outcome.strategy is strategy
            ),
            unresolved_gaps=sum(
                outcome.unresolved_gap_count
                for report in reports
                for outcome in report.outcomes
                if outcome.strategy is strategy
            ),
            covered_gaps=sum(
                outcome.covered_gap_count
                for report in reports
                for outcome in report.outcomes
                if outcome.strategy is strategy
            ),
            policy_excluded_cases=sum(
                bool(outcome.policy_excluded_tool_ids)
                for report in reports
                for outcome in report.outcomes
                if outcome.strategy is strategy
            ),
            replay_duration_ms=sum(
                outcome.replay_duration_ms
                for report in reports
                for outcome in report.outcomes
                if outcome.strategy is strategy
            ),
        )
        for strategy in COMPARISON_ORDER
    )


def _case_fingerprint(analysis: AnalysisRecord) -> str:
    payload = {
        "disease_id": analysis.disease_id,
        "target_mode": analysis.target_mode.value,
        "target_name": analysis.target_name,
        "canonical_smiles": analysis.canonical_smiles,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _actions(
    agent: AnalysisStageName,
    state: TrajectoryState,
    strategy: ComparisonStrategy,
    adaptive_selection: AdaptiveSelection | None = None,
) -> tuple[TrajectoryActionCandidate, ...]:
    tools = (
        TrajectoryActionCandidate(
            action_id="tool:admet_ai",
            kind=TrajectoryActionKind.CALL_TOOL,
            tool_id="admet_ai",
            reason_code="baseline_admet",
            objective="저장된 기본 ADMET 독성 관측을 확인한다.",
        ),
        TrajectoryActionCandidate(
            action_id="tool:ctoxpred2",
            kind=TrajectoryActionKind.CALL_TOOL,
            tool_id="ctoxpred2",
            reason_code="cardiac_ion_channel_evidence",
            objective="저장된 심장 이온통로 독성 관측을 확인한다.",
        ),
    )
    followup = TrajectoryActionCandidate(
        action_id="request:admet:cardiac_ion_channels",
        kind=TrajectoryActionKind.REQUEST_FOLLOWUP,
        target_agent=AnalysisStageName.ADMET,
        reason_code="cardiac_evidence_gap",
        objective="Decision이 심장 이온통로 근거 보완을 요청한다.",
    )
    stop = TrajectoryActionCandidate(
        action_id="stop:comparison",
        kind=TrajectoryActionKind.STOP,
        reason_code="policy_complete",
        objective="현재 정책의 관측을 마치고 비교 결과를 기록한다.",
    )
    baseline_seen = any(
        item in {"admet:context", "admet:baseline_failure"} for item in state.evidence_ids
    )
    cardiac_seen = any(
        item
        in {
            "admet:cardiac_ion_channels",
            "admet:recall_failure",
            "admet:cardiac_observation_failure",
        }
        for item in state.evidence_ids
    )
    if agent is AnalysisStageName.ADMET:
        available = tuple(
            action
            for action in tools
            if state.remaining_tool_calls > 0
            and (
                action.tool_id != "ctoxpred2"
                or strategy is ComparisonStrategy.ALL_TOOLS
                or (
                    strategy in {ComparisonStrategy.DECISION_RECALL, ComparisonStrategy.ADAPTIVE}
                    and state.remaining_recall_depth == 0
                    and (
                        strategy is not ComparisonStrategy.ADAPTIVE
                        or adaptive_selection is not None
                        and adaptive_selection.selected_action is not None
                    )
                )
            )
            and not (baseline_seen if action.tool_id == "admet_ai" else cardiac_seen)
        )
        if (
            strategy is ComparisonStrategy.ADAPTIVE
            and adaptive_selection is not None
            and adaptive_selection.selected_action is not None
        ):
            chosen_action = adaptive_selection.selected_action
            available = tuple(
                chosen_action if action.tool_id == "ctoxpred2" else action for action in available
            )
        return (*available, stop)
    if (
        strategy in {ComparisonStrategy.DECISION_RECALL, ComparisonStrategy.ADAPTIVE}
        and state.remaining_recall_depth > 0
        and not cardiac_seen
        and (
            strategy is not ComparisonStrategy.ADAPTIVE
            or adaptive_selection is not None
            and adaptive_selection.selected_action is not None
        )
    ):
        return (followup, stop)
    return (stop,)


def _exclusions(
    agent: AnalysisStageName,
    state: TrajectoryState,
    strategy: ComparisonStrategy,
    *,
    admet_version: str,
    ctox_version: str,
) -> tuple[TrajectoryCapabilityExclusion, ...]:
    if agent is not AnalysisStageName.ADMET:
        return ()
    baseline_seen = any(
        item in {"admet:context", "admet:baseline_failure"} for item in state.evidence_ids
    )
    cardiac_seen = any(
        item
        in {
            "admet:cardiac_ion_channels",
            "admet:recall_failure",
            "admet:cardiac_observation_failure",
        }
        for item in state.evidence_ids
    )
    admet_reason = None
    ctox_reason = None
    if state.remaining_tool_calls == 0:
        admet_reason = "budget_exhausted"
    elif baseline_seen:
        admet_reason = "already_observed"
    if strategy is ComparisonStrategy.BASELINE:
        ctox_reason = "policy_disabled"
    elif (
        strategy in {ComparisonStrategy.DECISION_RECALL, ComparisonStrategy.ADAPTIVE}
        and state.remaining_recall_depth > 0
    ):
        ctox_reason = "awaiting_decision_request"
    elif state.remaining_tool_calls == 0:
        ctox_reason = "budget_exhausted"
    elif cardiac_seen:
        ctox_reason = "already_observed"
    reasons = (
        ("admet_ai", f"admet_profile:{admet_version}", admet_reason),
        ("ctoxpred2", f"cardiac_ion_channels:{ctox_version}", ctox_reason),
    )
    return tuple(
        TrajectoryCapabilityExclusion(
            capability_id=capability_id,
            tool_id=tool_id,
            reason_code=reason,
        )
        for tool_id, capability_id, reason in reasons
        if reason is not None
    )


class PolicyComparisonRunner:
    """live provider 없이 검증된 snapshot 원본만 참조하는 파일럿 runner."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def run(self, manifest: PolicyComparisonManifest) -> PolicyComparisonReport:
        admet_repository = AdmetSnapshotRepository(self.session)
        ctox_repository = CtoxSnapshotRepository(self.session)
        admet = await admet_repository.load(manifest.admet_snapshot_id)
        ctox = await ctox_repository.load(manifest.ctox_snapshot_id)
        if admet is None or ctox is None:
            raise PolicyComparisonError("comparison_snapshot_missing")
        if admet.analysis_id != manifest.analysis_id or ctox.analysis_id != manifest.analysis_id:
            raise PolicyComparisonError("comparison_analysis_mismatch")
        # 한 case에 여러 입력이 있으면 임의의 첫 관측을 고르지 않는다.
        if len(admet.entries) != 1 or len(ctox.entries) != 1:
            raise PolicyComparisonError("comparison_requires_single_observation_per_tool")
        analysis = await self.session.get(AnalysisRecord, manifest.analysis_id)
        if analysis is None:
            raise PolicyComparisonError("comparison_analysis_missing")
        await admet_repository.verify_sources(admet)
        await ctox_repository.verify_sources(ctox)
        sources = (
            await verify_decision_sources(self.session, manifest.decision_sources, admet)
            if manifest.decision_sources is not None
            else None
        )
        outcomes = []
        for strategy in manifest.ordered_strategies():
            outcomes.append(
                await self._run_strategy(
                    manifest,
                    strategy,
                    admet,
                    ctox,
                    analysis.canonical_smiles,
                    sources=sources,
                )
            )
        return PolicyComparisonReport(
            analysis_id=manifest.analysis_id,
            admet_snapshot_id=admet.snapshot_id,
            ctox_snapshot_id=ctox.snapshot_id,
            policy_version=manifest.policy_version,
            case_fingerprint=_case_fingerprint(analysis),
            decision_sources=manifest.decision_sources,
            outcomes=tuple(outcomes),
        )

    async def run_with_adaptive(
        self,
        manifest: PolicyComparisonManifest,
        *,
        selector_input: AdaptiveSelectorInput,
        registry: ToolRegistry,
    ) -> AdaptiveComparisonReport:
        """같은 snapshot에 고정 정책 세 가지와 adaptive 정책을 나란히 replay한다."""
        selector_input = AdaptiveSelectorInput.model_validate_json(selector_input.model_dump_json())
        expected_gap_id = uuid5(manifest.analysis_id, "comparison:cardiac_ion_channel_evidence")
        if (
            selector_input.gap.gap_id != expected_gap_id
            or selector_input.gap.kind is not EvidenceGapKind.CARDIAC_ION_CHANNEL
            or selector_input.agent is not AnalysisStageName.ADMET
            or selector_input.available_inputs != ("canonical_smiles",)
            or selector_input.remaining_tool_calls != manifest.limits.max_tool_calls - 1
        ):
            raise PolicyComparisonError("adaptive_selector_context_mismatch")
        validate_capabilities(selector_input.capabilities, registry)
        registered_bindings = tuple(
            binding
            for capability in selector_input.capabilities
            if (binding := registry.get(capability.tool_id, capability.tool_version)) is not None
        )
        canonical_catalog = capabilities_for_bindings(registered_bindings)
        if tuple(sorted(selector_input.capabilities, key=lambda item: item.capability_id)) != tuple(
            sorted(canonical_catalog, key=lambda item: item.capability_id)
        ):
            raise PolicyComparisonError("adaptive_capability_catalog_mismatch")
        for pin in selector_input.allowed_tools:
            binding = registry.get(pin.tool_id, pin.version)
            if binding is None or AnalysisStageName.ADMET not in binding.agents:
                raise PolicyComparisonError("adaptive_tool_not_registered")
        admet = await AdmetSnapshotRepository(self.session).load(manifest.admet_snapshot_id)
        ctox = await CtoxSnapshotRepository(self.session).load(manifest.ctox_snapshot_id)
        analysis = await self.session.get(AnalysisRecord, manifest.analysis_id)
        if admet is None or ctox is None or analysis is None:
            raise PolicyComparisonError("comparison_snapshot_missing")
        selection = select_next_tool(selector_input)
        if selection.selected_action is not None and (
            selection.selected_action.tool_id != "ctoxpred2"
            or not any(
                pin.tool_id == "ctoxpred2" and pin.version == ctox.entries[0].tool_version
                for pin in selector_input.allowed_tools
            )
        ):
            raise PolicyComparisonError("adaptive_tool_snapshot_mismatch")
        fixed = await self.run(manifest)
        sources = (
            await verify_decision_sources(self.session, manifest.decision_sources, admet)
            if manifest.decision_sources is not None
            else None
        )
        adaptive = await self._run_strategy(
            manifest,
            ComparisonStrategy.ADAPTIVE,
            admet,
            ctox,
            analysis.canonical_smiles,
            sources=sources,
            selector_input=selector_input,
            adaptive_selection=selection,
        )
        return AdaptiveComparisonReport(fixed=fixed, adaptive=adaptive, selection=selection)

    async def _run_strategy(
        self,
        manifest: PolicyComparisonManifest,
        strategy: ComparisonStrategy,
        admet: AdmetObservationSnapshot,
        ctox: CtoxObservationSnapshot,
        canonical_smiles: str,
        *,
        sources: VerifiedDecisionSources | None = None,
        selector_input: AdaptiveSelectorInput | None = None,
        adaptive_selection: AdaptiveSelection | None = None,
    ) -> PolicyOutcome:
        episode_id = uuid4()
        profile = TrajectoryProfile(
            profile_id=f"comparison:{strategy.value}",
            policy_version=manifest.policy_version,
            execution_mode=TrajectoryExecutionMode.REPLAY,
            observation_snapshot_version=f"admet:{admet.snapshot_id};ctox:{ctox.snapshot_id}",
            selector_input_sha256=hashlib.sha256(
                selector_input.model_dump_json().encode()
            ).hexdigest()
            if selector_input is not None
            else None,
            limits=manifest.limits,
            max_steps=4,
        )
        repository = TrajectoryRepository(self.session)
        await repository.start_episode(manifest.analysis_id, profile, episode_id=episode_id)
        gap_id = uuid5(manifest.analysis_id, "comparison:cardiac_ion_channel_evidence")
        state = TrajectoryState(
            gap_ids=(gap_id,),
            remaining_tool_calls=manifest.limits.max_tool_calls,
            remaining_recall_depth=manifest.limits.max_recall_depth,
        )
        selected_tools: list[str] = []
        decision_inputs: list[PolicyDecisionInput] = []
        observed_refs: list[TrajectoryObservationReference] = []
        successes = failures = recalls = misses = replay_duration_ms = 0
        sequence = 0
        request_step_id: UUID | None = None
        response_step_id: UUID | None = None
        cardiac_result: CtoxToolResult | None = None
        cardiac_reason: str | None = None
        operations = ["admet_ai"]
        if strategy is ComparisonStrategy.ALL_TOOLS:
            operations.append("ctoxpred2")
        if strategy is ComparisonStrategy.DECISION_RECALL:
            operations.extend(("request_followup", "ctoxpred2"))
        if (
            strategy is ComparisonStrategy.ADAPTIVE
            and adaptive_selection is not None
            and adaptive_selection.selected_action is not None
        ):
            operations.extend(("request_followup", "ctoxpred2"))
        operations.append("stop")
        recall_path = "request_followup" in operations
        selector_payload = selector_input.model_dump_json() if selector_input is not None else None
        selector_digest = (
            hashlib.sha256(selector_payload.encode()).hexdigest()
            if selector_payload is not None
            else None
        )
        for operation in operations:
            sequence += 1
            step_id = uuid4()
            before = state
            observations: tuple[TrajectoryObservationReference, ...] = ()
            full_payload_sha256: str | None = None
            agent = (
                AnalysisStageName.DECISION
                if operation in {"request_followup", "stop"}
                else AnalysisStageName.ADMET
            )
            call_number = (
                2 if recall_path and (operation == "ctoxpred2" or operation == "stop") else 1
            )
            triggering_step_id = (
                request_step_id
                if recall_path and operation == "ctoxpred2"
                else response_step_id
                if recall_path and operation == "stop"
                else None
            )
            if agent is AnalysisStageName.DECISION:
                full_payload_json: str | None = None
                source_run_ids: tuple[UUID, ...] = ()
                if sources is not None:
                    target_items, target_restricted = target_evidence(sources.target)
                    admet_items, admet_restricted = admet_evidence(sources.admet)
                    dta_items, dta_restricted = dta_evidence(
                        sources.dta, sources.target, sources.assay_lineage
                    )
                    evidence = {**target_items, **admet_items}
                    if cardiac_result is not None:
                        evidence.update(
                            cardiac_evidence(
                                cardiac_result.predictions,
                                cardiac_result.limitations,
                                source_tool_call_id=ctox.entries[0].source_tool_call_id,
                                tool_version=ctox.entries[0].tool_version,
                            )
                        )
                    elif cardiac_reason is not None:
                        failure_key = (
                            "admet:recall_failure"
                            if recall_path
                            else "admet:cardiac_observation_failure"
                        )
                        evidence[failure_key] = {
                            "source_tool_call_id": str(ctox.entries[0].source_tool_call_id),
                            "reason_code": cardiac_reason,
                        }
                    evidence.update(dta_items)
                    simulated_feedback: dict[str, object] | None = None
                    if call_number == 2:
                        simulated_feedback = {
                            "status": "succeeded" if cardiac_result is not None else "failed",
                            "reason_code": cardiac_reason,
                            "source_tool_call_id": str(ctox.entries[0].source_tool_call_id),
                            "responds_to_call_number": 1,
                            "kind": "simulated_replay_feedback",
                        }
                    payload = decision_payload(
                        sources.target_input.case_input,
                        call_number,
                        manifest.limits
                        if recall_path
                        else manifest.limits.model_copy(update={"max_recall_depth": 0}),
                        evidence,
                        (),
                        target_restricted
                        or admet_restricted
                        or dta_restricted
                        or cardiac_reason is not None,
                        recall_feedback=simulated_feedback,
                    )
                    full_payload_json = decision_payload_json(payload)
                    full_payload_sha256 = decision_payload_sha256(payload)
                    source_run_ids = (
                        sources.target.run_id,
                        sources.admet.run_id,
                        sources.dta.run_id,
                    )
                decision_inputs.append(
                    PolicyDecisionInput(
                        projection_version="comparison-decision-input-v2"
                        if sources is not None
                        else "comparison-decision-input-v1",
                        call_number=call_number,
                        source_step_id=triggering_step_id,
                        evidence_ids=before.evidence_ids,
                        observation_refs=tuple(observed_refs),
                        unresolved_gap_ids=before.gap_ids,
                        source_run_ids=source_run_ids,
                        full_payload_json=full_payload_json,
                        full_payload_sha256=full_payload_sha256,
                    )
                )
            if operation == "request_followup":
                selected = "request:admet:cardiac_ion_channels"
                request_step_id = step_id
                state = state.model_copy(
                    update={
                        "remaining_recall_depth": state.remaining_recall_depth - 1,
                    }
                )
                recalls += 1
            elif operation == "stop":
                selected = "stop:comparison"
                state = state.model_copy(update={"decision": "evidence_review_complete"})
            else:
                selected = (
                    adaptive_selection.selected_action.action_id
                    if strategy is ComparisonStrategy.ADAPTIVE
                    and operation == "ctoxpred2"
                    and adaptive_selection is not None
                    and adaptive_selection.selected_action is not None
                    else f"tool:{operation}"
                )
                if operation == "ctoxpred2" and recall_path:
                    response_step_id = step_id
                entry = admet.entries[0] if operation == "admet_ai" else ctox.entries[0]
                observation: ToolObservation[AdmetToolResult] | ToolObservation[CtoxToolResult]
                if operation == "admet_ai":
                    admet_observation = await AdmetReplayExecutor(self.session).execute(
                        admet.snapshot_id,
                        tool_call_id=uuid4(),
                        request=ToolRequest[AdmetToolArguments](
                            request_id=uuid4(),
                            run_id=uuid4(),
                            tool_id="admet_ai",
                            tool_version=entry.tool_version,
                            arguments=AdmetToolArguments(canonical_smiles=canonical_smiles),
                            objective="동일 case의 ADMET 독성 관측을 replay한다.",
                        ),
                    )
                    observation = admet_observation
                else:
                    ctox_observation = await CtoxReplayExecutor(self.session).execute(
                        ctox.snapshot_id,
                        tool_call_id=uuid4(),
                        request=ToolRequest[CtoxToolArguments](
                            request_id=uuid4(),
                            run_id=uuid4(),
                            tool_id="ctoxpred2",
                            tool_version=entry.tool_version,
                            arguments=CtoxToolArguments(canonical_smiles=canonical_smiles),
                            objective="동일 case의 심장 이온통로 관측을 replay한다.",
                        ),
                        expected_analysis_id=manifest.analysis_id,
                    )
                    observation = ctox_observation
                success = observation.status == "succeeded"
                if operation == "ctoxpred2":
                    cardiac_result = (
                        CtoxToolResult.model_validate(observation.result.model_dump())
                        if observation.result is not None
                        else None
                    )
                    cardiac_reason = observation.error.code if observation.error else None
                replay_duration_ms += observation.execution_metadata.duration_ms
                reason = observation.error.code if observation.error is not None else None
                miss = reason is not None and reason.startswith("replay_")
                misses += int(miss)
                observations = (
                    TrajectoryObservationReference(
                        tool_call_id=entry.source_tool_call_id if not miss else None,
                        artifact=observation.raw_result,
                        outcome="succeeded" if success else ("replay_miss" if miss else "failed"),
                        reason_code=reason,
                    ),
                )
                observed_refs.extend(observations)
                state = state.model_copy(
                    update={
                        "evidence_ids": (
                            *state.evidence_ids,
                            ("admet:context" if success else "admet:baseline_failure")
                            if operation == "admet_ai"
                            else (
                                "admet:cardiac_ion_channels"
                                if success
                                else (
                                    "admet:recall_failure"
                                    if recall_path
                                    else "admet:cardiac_observation_failure"
                                )
                            ),
                        ),
                        "gap_ids": tuple(item for item in state.gap_ids if item != gap_id)
                        if operation == "ctoxpred2" and success
                        else state.gap_ids,
                        "remaining_tool_calls": state.remaining_tool_calls - 1,
                    }
                )
                selected_tools.append(operation)
                successes += int(success)
                failures += int(not success)
            await repository.append_step(
                TrajectoryStep(
                    episode_id=episode_id,
                    step_id=step_id,
                    triggering_step_id=triggering_step_id,
                    sequence_number=sequence,
                    agent_name=agent,
                    agent_call_number=call_number,
                    state_before=before,
                    available_actions=_actions(agent, before, strategy, adaptive_selection),
                    excluded_capabilities=(
                        tuple(
                            TrajectoryCapabilityExclusion(
                                capability_id=item.capability_id,
                                tool_id=item.tool_id,
                                endpoint_id=item.endpoint_id,
                                reason_code=item.reason_code,
                            )
                            for item in adaptive_selection.candidates
                            if not item.selected
                        )
                        if strategy is ComparisonStrategy.ADAPTIVE
                        and adaptive_selection is not None
                        and (operation == "ctoxpred2" or operation == "stop" and not recall_path)
                        else _exclusions(
                            agent,
                            before,
                            strategy,
                            admet_version=admet.entries[0].tool_version,
                            ctox_version=ctox.entries[0].tool_version,
                        )
                    ),
                    selected_action_id=selected,
                    selector_input_json=selector_payload
                    if strategy is ComparisonStrategy.ADAPTIVE
                    and (operation == "ctoxpred2" or operation == "stop" and not recall_path)
                    else None,
                    selector_input_sha256=selector_digest
                    if strategy is ComparisonStrategy.ADAPTIVE
                    and (operation == "ctoxpred2" or operation == "stop" and not recall_path)
                    else None,
                    observations=observations,
                    state_after=state,
                )
            )
        await repository.finish_episode(episode_id)
        validation = await validate_stored_episode(self.session, episode_id)
        return PolicyOutcome(
            strategy=strategy,
            episode_id=episode_id,
            selected_tool_ids=tuple(selected_tools),
            policy_excluded_tool_ids=("ctoxpred2",)
            if strategy is ComparisonStrategy.BASELINE
            else (),
            attempted_observations=len(selected_tools),
            successful_observations=successes,
            failed_observations=failures,
            recall_requests=recalls,
            replay_duration_ms=replay_duration_ms,
            replay_misses=misses,
            covered_gap_count=1 - len(state.gap_ids),
            unresolved_gap_count=len(state.gap_ids),
            validation_verdict=validation.verdict,
            validation_reason_codes=tuple(issue.reason_code for issue in validation.issues),
            decision_inputs=tuple(decision_inputs),
        )
