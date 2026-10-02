"""전문 관측의 작은 projection으로 최종 연구 판단을 한 번 생성한다."""

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evidrug_api.admet.agent import AdmetAgentResult
from evidrug_api.admet.recall import AdmetRecallResult
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.ctoxpred2.contracts import CTOX_TOOL_VERSION
from evidrug_api.ctoxpred2.repository import CtoxRepository
from evidrug_api.ctoxpred2.tables import CtoxExecutionRecord
from evidrug_api.decision.dta_lineage import verify_dta_assay_lineage
from evidrug_api.decision.policy import DecisionMetadata, calculate_metadata
from evidrug_api.decision.projection import (
    admet_evidence,
    cardiac_evidence,
    decision_payload,
    decision_payload_json,
    dta_evidence,
    target_evidence,
)
from evidrug_api.decision.recall import DecisionRecallRequest
from evidrug_api.decision.validation import PolicyViolation, validate_policy
from evidrug_api.dta.agent import DtaAgentResult
from evidrug_api.execution_contracts.agent import (
    AgentInput,
    AgentOutput,
    AgentOutputStatus,
    AgentWarning,
)
from evidrug_api.execution_contracts.common import (
    ComponentVersion,
    ContractModel,
    ExecutionError,
    ExecutionMetadata,
    ExecutionUsage,
    TokenUsage,
)
from evidrug_api.openai_gateway.client import DaconOpenAIClient
from evidrug_api.openai_gateway.diagnostics import model_call_diagnostic
from evidrug_api.openai_gateway.models import ResponseUsage
from evidrug_api.orchestration.contracts import AgentExecutionResult
from evidrug_api.orchestration.tables import AgentRunRecord
from evidrug_api.orchestration.upstream import InvalidUpstream, load_upstream
from evidrug_api.target_hypothesis.contracts import TargetHypothesisResult
from evidrug_api.trajectory.ctox_snapshot import CtoxSnapshotRepository


class DecisionNextAction(ContractModel):
    """아직 수행되지 않은 후속 조치와 판정 변경 조건을 분리해 보존한다."""

    status: Literal["proposed"]
    action: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)
    decision_impact: str = Field(min_length=1, max_length=500)


class LegacyDecisionAssessment(ContractModel):
    decision_phase: Literal["final", "request_recall"]
    verdict: Literal["go", "conditional_go", "no_go"]
    rationale: str = Field(min_length=1, max_length=2500)
    used_evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    conflicts: tuple[str, ...] = Field(max_length=8)
    gaps: tuple[str, ...] = Field(max_length=8)
    # 기본값은 v3 저장 결과를 다시 읽기 위한 호환 경로다.
    # 새 생성 결과는 아래 검증에서 1~3개를 요구한다.
    next_actions: tuple[DecisionNextAction, ...] = Field(default=(), max_length=3)
    recall_request: DecisionRecallRequest | None = None

    @model_validator(mode="after")
    def validate_recall_phase(self) -> "LegacyDecisionAssessment":
        if (self.decision_phase == "request_recall") != (self.recall_request is not None):
            raise ValueError("recall request must match decision phase")
        if self.decision_phase == "request_recall" and self.verdict != "conditional_go":
            raise ValueError("recall request requires provisional conditional_go")
        if self.verdict == "conditional_go" and not self.gaps:
            raise ValueError("conditional_go requires a decision-changing gap")
        return self


class DecisionKeyValue(ContractModel):
    label: str = Field(min_length=1, max_length=200)
    value: float = Field(strict=True, allow_inf_nan=False)
    unit: str | None


class DecisionArea(ContractModel):
    status: Literal["supported", "unresolved", "unavailable"]
    summary: str = Field(min_length=1, max_length=1000)
    key_values: tuple[DecisionKeyValue, ...] = Field(max_length=6)


class DecisionAdmeArea(ContractModel):
    status: Literal["informative", "unavailable"]
    summary: str = Field(min_length=1, max_length=1000)
    key_values: tuple[DecisionKeyValue, ...] = Field(max_length=6)


class DecisionAreas(ContractModel):
    target: DecisionArea
    dta: DecisionArea
    adme: DecisionAdmeArea
    safety: DecisionArea


class DecisionAssessment(LegacyDecisionAssessment):
    headline: str = Field(min_length=1, max_length=150)
    assessment: DecisionAreas
    key_strengths: tuple[str, ...] = Field(max_length=3)
    key_concerns: tuple[str, ...] = Field(max_length=3)
    rationale: str = Field(min_length=1, max_length=300)
    next_actions: tuple[DecisionNextAction, ...] = Field(min_length=1, max_length=3)
    recall_request: DecisionRecallRequest | None


class DecisionRecallGeneration(ContractModel):
    """strict 생성 요청에서 default 없이 모든 recall 필드를 요구한다."""

    schema_version: Literal["1"]
    gap_kind: Literal["cardiac_ion_channel_evidence"]
    objective: str = Field(min_length=1, max_length=500)
    reason_code: Literal[
        "baseline_herg_signal", "baseline_herg_uncertainty", "cardiac_evidence_gap"
    ]
    required_endpoints: tuple[str, ...]
    priority: Literal["normal", "high"]


class DecisionAssessmentGeneration(ContractModel):
    """공개 계약을 바꾸지 않는 Decision 교정 호출 전용 strict schema."""

    decision_phase: Literal["final", "request_recall"]
    verdict: Literal["go", "conditional_go", "no_go"]
    headline: str = Field(min_length=1, max_length=150)
    assessment: DecisionAreas
    key_strengths: tuple[str, ...] = Field(max_length=3)
    key_concerns: tuple[str, ...] = Field(max_length=3)
    rationale: str = Field(min_length=1, max_length=300)
    used_evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    conflicts: tuple[str, ...] = Field(max_length=8)
    gaps: tuple[str, ...] = Field(max_length=8)
    next_actions: tuple[DecisionNextAction, ...] = Field(min_length=1, max_length=3)
    recall_request: DecisionRecallGeneration | None


class DecisionResult(ContractModel):
    assessment: DecisionAssessment | LegacyDecisionAssessment
    server_metadata: DecisionMetadata | None = None
    source_run_ids: tuple[UUID, ...]
    missing_stages: tuple[AnalysisStageName, ...]
    # 버전 필드가 없는 과거 저장 결과를 새 계약으로 오인하지 않는다.
    context_version: str = "decision-context-v4"
    scope: Literal["research_prioritization_only"] = "research_prioritization_only"


DECISION_PROMPT_VERSION = "decision-gated-synthesis-v5.7"
DECISION_MAX_OUTPUT_TOKENS = 8192
DECISION_MAX_INPUT_BYTES = 64 * 1024
DECISION_CORRECTION_REASON_CODES = frozenset(
    {
        "decision_json_invalid",
        "decision_response_incomplete",
        "decision_key_value_out_of_range",
        "decision_key_value_label_mismatch",
    }
)

_INLINE_EVIDENCE_MARKER = re.compile(r"\s*\[(?:target|admet|dta|decision):[^\]\n]{1,200}\]")
_DECIMAL_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<number>[+-]?\d+\.\d{3,}(?:[eE][+-]?\d+)?)(?![A-Za-z0-9_.])"
)


def remove_inline_evidence_markers[AssessmentT: LegacyDecisionAssessment](
    assessment: AssessmentT,
) -> tuple[AssessmentT, int]:
    """본문의 대괄호 근거 표기만 제거한다. 구조화된 인용 ID와 판단 내용은 유지한다."""

    removed = 0

    def clean(value: str) -> str:
        nonlocal removed
        cleaned, count = _INLINE_EVIDENCE_MARKER.subn("", value)
        removed += count
        return " ".join(cleaned.split()) if count else value

    if not isinstance(assessment, DecisionAssessment) and not any(
        _INLINE_EVIDENCE_MARKER.search(value)
        for value in (
            assessment.rationale,
            *assessment.conflicts,
            *assessment.gaps,
            *(item.action for item in assessment.next_actions),
            *(item.rationale for item in assessment.next_actions),
            *(item.decision_impact for item in assessment.next_actions),
        )
    ):
        return assessment, 0
    conflicts = tuple(filter(None, (clean(value) for value in assessment.conflicts)))
    gaps = tuple(filter(None, (clean(value) for value in assessment.gaps)))
    next_actions = tuple(
        item.model_copy(
            update={
                "action": clean(item.action),
                "rationale": clean(item.rationale),
                "decision_impact": clean(item.decision_impact),
            }
        )
        for item in assessment.next_actions
    )
    cleaned = assessment.model_copy(
        update={
            "rationale": clean(assessment.rationale),
            "conflicts": conflicts,
            "gaps": gaps,
            "next_actions": next_actions,
        }
    )
    if isinstance(cleaned, DecisionAssessment):
        cleaned = cleaned.model_copy(
            update={
                "headline": clean(cleaned.headline),
                "key_strengths": tuple(clean(v) for v in cleaned.key_strengths),
                "key_concerns": tuple(clean(v) for v in cleaned.key_concerns),
                "assessment": cleaned.assessment.model_copy(
                    update={
                        area: getattr(cleaned.assessment, area).model_copy(
                            update={
                                "summary": clean(getattr(cleaned.assessment, area).summary),
                            }
                        )
                        for area in ("target", "dta", "adme", "safety")
                    }
                ),
            }
        )
        try:
            DecisionAssessment.model_validate(cleaned.model_dump())
        except ValidationError as error:
            raise InvalidDecisionOutput("decision_prose_empty_after_citation_cleanup") from error
    if (
        not cleaned.rationale
        or (cleaned.verdict == "conditional_go" and not cleaned.gaps)
        or any(
            not value
            for item in cleaned.next_actions
            for value in (item.action, item.rationale, item.decision_impact)
        )
    ):
        raise InvalidDecisionOutput("decision_prose_empty_after_citation_cleanup")
    return cleaned, removed


def normalize_reader_numbers[AssessmentT: LegacyDecisionAssessment](
    assessment: AssessmentT,
) -> tuple[AssessmentT, int]:
    """원 근거값은 유지하고 독자용 Decision 문장의 긴 소수만 읽기 좋게 줄인다."""

    changed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        raw = match.group("number")
        try:
            value = Decimal(raw)
        except InvalidOperation:
            return raw
        absolute = abs(value)
        if value != 0 and absolute < Decimal("0.005"):
            rendered = f"{value:.1E}".lower()
            mantissa, exponent = rendered.split("e")
            rendered = f"{mantissa}e{int(exponent):+d}"
        else:
            rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            rendered = format(rounded, "f").rstrip("0").rstrip(".")
            if rendered in {"-0", "+0", ""}:
                rendered = "0"
        changed += rendered != raw
        return rendered

    def clean(value: str) -> str:
        return _DECIMAL_NUMBER.sub(replace, value)

    next_actions = tuple(
        item.model_copy(
            update={
                "action": clean(item.action),
                "rationale": clean(item.rationale),
                "decision_impact": clean(item.decision_impact),
            }
        )
        for item in assessment.next_actions
    )
    normalized = assessment.model_copy(
        update={
            "rationale": clean(assessment.rationale),
            "conflicts": tuple(clean(value) for value in assessment.conflicts),
            "gaps": tuple(clean(value) for value in assessment.gaps),
            "next_actions": next_actions,
        }
    )
    if isinstance(normalized, DecisionAssessment):
        normalized = normalized.model_copy(
            update={
                "headline": clean(normalized.headline),
                "key_strengths": tuple(clean(v) for v in normalized.key_strengths),
                "key_concerns": tuple(clean(v) for v in normalized.key_concerns),
                "assessment": normalized.assessment.model_copy(
                    update={
                        area: getattr(normalized.assessment, area).model_copy(
                            update={
                                "summary": clean(getattr(normalized.assessment, area).summary),
                            }
                        )
                        for area in ("target", "dta", "adme", "safety")
                    }
                ),
            }
        )
    return normalized, changed


INSTRUCTIONS = (
    Path(__file__).with_name("prompt_v5_7.txt").read_text(encoding="utf-8").removesuffix("\n")
)


class InvalidDecisionOutput(ValueError):
    """원문이나 입력을 포함하지 않는 고정 진단 코드."""


def _merge_usage(current: ExecutionUsage, generated_usage: ResponseUsage | None) -> ExecutionUsage:
    """교정 호출을 포함한 알려진 모델 사용량과 외부 요청 수를 합산한다."""
    token_usage = current.token_usage
    if generated_usage is not None:
        added = TokenUsage(
            input_tokens=generated_usage.input_tokens,
            output_tokens=generated_usage.output_tokens,
            total_tokens=generated_usage.total_tokens,
        )
        token_usage = (
            TokenUsage(
                input_tokens=token_usage.input_tokens + added.input_tokens,
                output_tokens=token_usage.output_tokens + added.output_tokens,
                total_tokens=token_usage.total_tokens + added.total_tokens,
            )
            if token_usage is not None
            else added
        )
    return ExecutionUsage(
        external_requests=current.external_requests + 1,
        token_usage=token_usage,
    )


def _correction_instructions(reason_code: str) -> str:
    """거부 원문을 재전송하지 않고 승인된 검증 사유만 교정 지시로 추가한다."""
    guidance = {
        "decision_json_invalid": (
            "Return one complete and concise object matching the supplied strict JSON schema."
        ),
        "decision_response_incomplete": (
            "The previous response did not finish. Return a shorter complete object within the "
            "output limit. Keep every prose field concise and return JSON only."
        ),
        "decision_key_value_out_of_range": (
            "Set key_values to [] in target, dta, adme, and safety. Do not emit numeric "
            "key_values; preserve the evidence-based conclusion in concise summaries."
        ),
        "decision_key_value_label_mismatch": (
            "Set key_values to [] in target, dta, adme, and safety. Do not emit numeric "
            "key_values; preserve the evidence-based conclusion in concise summaries."
        ),
    }[reason_code]
    return (
        INSTRUCTIONS
        + "\n\nThe previous response was rejected by the server validator with reason code "
        + reason_code
        + ". Regenerate the complete JSON object once from the supplied evidence. "
        + guidance
        + " Return JSON only."
    )


def decision_input_size(prompt: str, instructions: str = INSTRUCTIONS) -> int:
    """Decision 호출 전 UTF-8 입력과 고정 여유분의 byte 상한을 계산한다."""
    return len((instructions + prompt).encode("utf-8")) + 512


def validate_assessment(
    text: str,
    status: str,
    evidence: dict[str, object],
    restricted: bool,
    *,
    allow_recall: bool = False,
    metadata: DecisionMetadata | None = None,
    boundary: dict[str, object] | None = None,
) -> DecisionAssessment:
    """형식/인용/정책 거부를 구분하며 실패한 내용을 임의로 보정하지 않는다."""
    if status != "completed":
        raise InvalidDecisionOutput("decision_response_incomplete")
    try:
        json.loads(text)
    except ValueError as error:
        raise InvalidDecisionOutput("decision_json_invalid") from error
    try:
        assessment = DecisionAssessment.model_validate_json(text)
    except ValidationError as error:
        raise InvalidDecisionOutput("decision_schema_invalid") from error
    if not set(assessment.used_evidence_ids) <= set(evidence):
        raise InvalidDecisionOutput("decision_citation_unknown")
    available = {key.split(":", 1)[0] for key in evidence}
    cited = {key.split(":", 1)[0] for key in assessment.used_evidence_ids}
    if not available <= cited:
        raise InvalidDecisionOutput("decision_specialist_citation_missing")
    if restricted and assessment.verdict == "go":
        raise InvalidDecisionOutput("decision_go_restricted")
    if assessment.recall_request is not None and not allow_recall:
        raise InvalidDecisionOutput("decision_recall_not_allowed")
    if not assessment.next_actions:
        raise InvalidDecisionOutput("decision_next_actions_missing")
    try:
        validate_policy(
            assessment.model_dump(mode="json"),
            evidence,
            metadata or calculate_metadata(evidence),
            boundary,
        )
    except PolicyViolation as error:
        raise InvalidDecisionOutput(str(error)) from error
    return assessment


class DecisionAgent:
    """완료와 Go를 구분하며 오류 응답의 토큰 사용량도 보존한다."""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], client: DaconOpenAIClient
    ) -> None:
        self.factory, self.client = factory, client

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        """검증한 upstream만 종합하고 한 번의 제한된 LLM 호출로 마무리한다."""
        started = datetime.now(UTC)
        if agent_input.agent_name != AnalysisStageName.DECISION:
            raise InvalidUpstream("decision_agent_mismatch")
        evidence, missing, restricted = await self._context(agent_input)
        payload = decision_payload(
            agent_input.case_input,
            agent_input.attempt,
            agent_input.execution_limits,
            evidence,
            missing,
            restricted,
            recall_feedback=agent_input.recall_feedback.model_dump(mode="json")
            if agent_input.recall_feedback
            else None,
        )
        allow_recall = bool(payload["allow_recall"])
        metadata = calculate_metadata(evidence, tuple(stage.value for stage in missing))
        restricted = restricted or bool(metadata.go_restrictions)
        boundary = cast(dict[str, object] | None, payload["dta_reference_boundary"])
        prompt = decision_payload_json(payload)
        output_limit = DECISION_MAX_OUTPUT_TOKENS
        input_bound = decision_input_size(prompt)
        token_limit = agent_input.execution_limits.max_total_tokens
        result = None
        usage = ExecutionUsage()
        components: tuple[ComponentVersion, ...] = (
            ComponentVersion(component="decision_prompt", version=DECISION_PROMPT_VERSION),
        )
        error_code = None
        warnings: tuple[AgentWarning, ...] = ()
        if input_bound > DECISION_MAX_INPUT_BYTES or (
            token_limit is not None and input_bound + output_limit > token_limit
        ):
            error_code = "decision_input_budget_exceeded"
        else:
            instructions = INSTRUCTIONS
            correction_reason: str | None = None
            while True:
                try:
                    generated = await self.client.generate_text(
                        prompt,
                        instructions=instructions,
                        max_output_tokens=output_limit,
                        output_schema=DecisionAssessmentGeneration,
                    )
                except Exception as error:
                    usage = usage.model_copy(
                        update={"external_requests": usage.external_requests + 1}
                    )
                    error_code = "decision_model_unavailable"
                    warnings = (
                        AgentWarning(
                            code=model_call_diagnostic(error),
                            message="Decision 모델 호출 실패 유형입니다. 원문은 저장하지 않습니다.",
                        ),
                    )
                    break
                components += (
                    ComponentVersion(component="language_model", version=generated.model),
                )
                usage = _merge_usage(usage, generated.usage)
                try:
                    assessment = validate_assessment(
                        generated.text,
                        generated.status,
                        evidence,
                        restricted,
                        allow_recall=allow_recall,
                        metadata=metadata,
                        boundary=boundary,
                    )
                    assessment, removed_markers = remove_inline_evidence_markers(assessment)
                    assessment, normalized_numbers = normalize_reader_numbers(assessment)
                    warning_items: list[AgentWarning] = []
                    warning_items.extend(
                        AgentWarning(code=code, message="Decision 표시 정책 경고입니다.")
                        for code in validate_policy(
                            assessment.model_dump(mode="json"), evidence, metadata, boundary
                        )
                    )
                    if removed_markers:
                        warning_items.append(
                            AgentWarning(
                                code="decision_inline_citations_removed",
                                message="독자용 문장에서 내부 근거 ID 표기를 제거했습니다.",
                            )
                        )
                    if normalized_numbers:
                        warning_items.append(
                            AgentWarning(
                                code="decision_numeric_display_normalized",
                                message=(
                                    "독자용 문장의 긴 소수를 표시 정밀도에 맞게 정규화했습니다."
                                ),
                            )
                        )
                    warnings = tuple(warning_items)
                    if correction_reason is not None:
                        warnings += (
                            AgentWarning(
                                code="decision_output_correction_applied",
                                message=(
                                    "승인된 Decision 출력 검증 실패 후 "
                                    "교정 재호출 1회를 적용했습니다."
                                ),
                            ),
                        )
                    result = DecisionResult(
                        context_version="decision-context-v5.7",
                        server_metadata=metadata,
                        assessment=assessment,
                        source_run_ids=tuple(ref.run_id for ref in agent_input.upstream_outputs),
                        missing_stages=missing,
                    )
                    break
                except InvalidDecisionOutput as error:
                    reason_code = str(error)
                    if (
                        correction_reason is None
                        and reason_code in DECISION_CORRECTION_REASON_CODES
                    ):
                        candidate_instructions = _correction_instructions(reason_code)
                        correction_input_bound = decision_input_size(prompt, candidate_instructions)
                        used_tokens = (
                            usage.token_usage.total_tokens if usage.token_usage is not None else 0
                        )
                        correction_within_token_budget = token_limit is None or (
                            used_tokens + correction_input_bound + output_limit <= token_limit
                        )
                        if (
                            correction_input_bound <= DECISION_MAX_INPUT_BYTES
                            and correction_within_token_budget
                        ):
                            correction_reason = reason_code
                            instructions = candidate_instructions
                            components += (
                                ComponentVersion(
                                    component="decision_output_correction",
                                    version=reason_code,
                                ),
                            )
                            continue
                    error_code = "decision_invalid_output"
                    warnings = (
                        AgentWarning(
                            code=reason_code, message="Decision 출력 검증 거부 사유입니다."
                        ),
                    )
                    break
        finished = datetime.now(UTC)
        output = AgentOutput[DecisionResult](
            analysis_id=agent_input.analysis_id,
            run_id=agent_input.run_id,
            agent_name=agent_input.agent_name,
            status=AgentOutputStatus.FAILED if error_code else AgentOutputStatus.COMPLETED,
            result=result,
            warnings=warnings,
            error=ExecutionError(
                code=error_code,
                message="근거에 맞는 최종 연구 판단을 생성하지 못했습니다.",
                retryable=False,
            )
            if error_code
            else None,
            execution_metadata=ExecutionMetadata(
                started_at=started,
                finished_at=finished,
                duration_ms=int((finished - started).total_seconds() * 1000),
                implementation_version="decision-agent-v6.1",
                components=components,
                usage=usage,
            ),
        )
        return AgentExecutionResult(cast(AgentOutput[BaseModel], output))

    async def _context(
        self, agent_input: AgentInput
    ) -> tuple[dict[str, object], tuple[AnalysisStageName, ...], bool]:
        """서열·전체 Target evidence·manifest를 복사하지 않고 핵심 값/출처를 투영한다."""
        evidence: dict[str, object] = {}
        refs = {ref.agent_name: ref for ref in agent_input.upstream_outputs}
        admet_refs = [
            ref for ref in agent_input.upstream_outputs if ref.agent_name is AnalysisStageName.ADMET
        ]
        feedback = agent_input.recall_feedback
        required = (
            AnalysisStageName.TARGET_HYPOTHESIS,
            AnalysisStageName.ADMET,
            AnalysisStageName.DTA,
        )
        if (
            not refs
            or len(refs) != len(agent_input.upstream_outputs) - (1 if len(admet_refs) == 2 else 0)
            or not set(refs) <= set(required)
            or len(admet_refs) > 2
            or (agent_input.attempt == 2) != (feedback is not None)
            or (feedback is not None and feedback.status == "succeeded" and len(admet_refs) != 2)
            or (feedback is not None and feedback.status == "failed" and len(admet_refs) != 1)
        ):
            raise InvalidUpstream("decision_upstream_set_mismatch")
        if admet_refs:
            refs[AnalysisStageName.ADMET] = admet_refs[0]
        missing = tuple(stage for stage in required if stage not in refs)
        restricted = bool(missing)
        target_ref = refs.get(AnalysisStageName.TARGET_HYPOTHESIS)
        if target_ref:
            target = await load_upstream(
                self.factory, agent_input, target_ref, AgentOutput[TargetHypothesisResult]
            )
            projected, limited = target_evidence(target)
            evidence.update(projected)
            restricted |= limited
        admet_ref = refs.get(AnalysisStageName.ADMET)
        if admet_ref:
            admet = await load_upstream(
                self.factory, agent_input, admet_ref, AgentOutput[AdmetAgentResult]
            )
            projected, limited = admet_evidence(admet)
            evidence.update(projected)
            restricted |= limited
        if feedback is not None:
            async with self.factory() as session:
                recall_run = await session.get(AgentRunRecord, feedback.run_id)
                if (
                    recall_run is None
                    or recall_run.analysis_id != agent_input.analysis_id
                    or recall_run.agent_name != AnalysisStageName.ADMET
                    or recall_run.attempt != 2
                    or recall_run.status
                    != ("completed" if feedback.status == "succeeded" else "failed")
                    or (
                        feedback.status == "failed"
                        and recall_run.error_code != feedback.reason_code
                    )
                ):
                    raise InvalidUpstream("decision_recall_feedback_mismatch")
            if feedback.status == "failed":
                evidence["admet:recall_failure"] = {
                    "run_id": str(feedback.run_id),
                    "gap_kind": feedback.request.gap_kind.value,
                    "reason_code": feedback.reason_code,
                }
                restricted = True
            else:
                recall_ref = admet_refs[1]
                if recall_ref.run_id != feedback.run_id:
                    raise InvalidUpstream("decision_recall_reference_mismatch")
                recall = await load_upstream(
                    self.factory, agent_input, recall_ref, AgentOutput[AdmetRecallResult]
                )
                assert recall.result is not None
                async with self.factory() as session:
                    ctox_source = await session.get(
                        CtoxExecutionRecord, recall.result.source_tool_call_id
                    )
                    saved = await CtoxRepository(session).load(recall.result.source_tool_call_id)
                    snapshot_valid = recall.result.execution_mode == "live"
                    if recall.result.execution_mode == "replay":
                        snapshot = (
                            await CtoxSnapshotRepository(session).load(
                                recall.result.replay_snapshot_id
                            )
                            if recall.result.replay_snapshot_id
                            else None
                        )
                        entry = (
                            next(
                                (
                                    item
                                    for item in snapshot.entries
                                    if item.source_tool_call_id == recall.result.source_tool_call_id
                                ),
                                None,
                            )
                            if snapshot is not None
                            else None
                        )
                        snapshot_valid = (
                            snapshot is not None
                            and snapshot.analysis_id == agent_input.analysis_id
                            and entry is not None
                            and entry.status == "succeeded"
                            and ctox_source is not None
                            and entry.source_run_id == ctox_source.run_id
                            and saved is not None
                            and entry.result_sha256
                            == hashlib.sha256(saved.model_dump_json().encode()).hexdigest()
                            and entry.model == saved.model
                        )
                    if (
                        ctox_source is None
                        or ctox_source.analysis_id != agent_input.analysis_id
                        or (
                            recall.result.execution_mode == "live"
                            and (
                                ctox_source.run_id != recall.run_id
                                or recall.result.source_tool_call_id != recall.result.tool_call_id
                                or recall.result.replay_snapshot_id is not None
                            )
                        )
                        or not snapshot_valid
                        or saved is None
                        or saved.predictions != recall.result.predictions
                        or recall.result.tool_version != CTOX_TOOL_VERSION
                    ):
                        raise InvalidUpstream("decision_recall_source_mismatch")
                evidence.update(
                    cardiac_evidence(
                        recall.result.predictions,
                        recall.result.limitations,
                        source_tool_call_id=recall.result.source_tool_call_id,
                        tool_version=recall.result.tool_version,
                        agent_run_id=recall.run_id,
                    )
                )
        dta_ref = refs.get(AnalysisStageName.DTA)
        if dta_ref:
            dta = await load_upstream(
                self.factory, agent_input, dta_ref, AgentOutput[DtaAgentResult]
            )
            assert dta.result is not None
            async with self.factory() as session:
                assay_lineage = await verify_dta_assay_lineage(
                    session,
                    analysis_id=agent_input.analysis_id,
                    run_id=dta.run_id,
                    canonical_smiles=agent_input.case_input.canonical_smiles,
                    result=dta.result,
                )
            projected, limited = dta_evidence(dta, target if target_ref else None, assay_lineage)
            evidence.update(projected)
            restricted |= limited
        return evidence, missing, restricted
