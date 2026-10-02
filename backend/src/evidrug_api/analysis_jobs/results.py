"""저장된 전문 출력만 검증·투영한다. 실행기나 외부 클라이언트를 생성하지 않는다."""

import hashlib
import re
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError

from evidrug_api.admet.agent import AdmetAgentResult
from evidrug_api.admet.recall import AdmetRecallResult
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.analysis_jobs.repository import AnalysisRepository
from evidrug_api.analysis_jobs.result_models import (
    AdmetDomainSummary,
    AdmetEndpointSummary,
    AdmetSummary,
    AgentCallRequestSummary,
    AgentCallSummary,
    CardiacChannelSummary,
    CardiacRecallSummary,
    DecisionSummary,
    DtaCandidateSummary,
    DtaSummary,
    PublicRun,
    SpecialistResultsResponse,
)
from evidrug_api.analysis_jobs.service import read_owned_analysis
from evidrug_api.decision.agent import DecisionResult
from evidrug_api.dta.agent import DtaAgentResult
from evidrug_api.execution_contracts.agent import AgentInput, AgentOutput, AgentOutputStatus
from evidrug_api.execution_contracts.recall import DecisionRecallRequest
from evidrug_api.orchestration.tables import AgentRunRecord


def _safe_code(value: str | None) -> str | None:
    return value if value and re.fullmatch(r"[a-z][a-z0-9_]{0,119}", value) else None


def project_run[StoredT: BaseModel, PublicT: BaseModel](
    row: AgentRunRecord | None,
    output_type: type[AgentOutput[StoredT]],
    project: Callable[[StoredT], PublicT | None],
) -> PublicRun[PublicT]:
    """손상/과거 출력은 격리하고 공개 필드만 반환한다. 원문을 로그에 남기지 않는다."""
    response: PublicRun[PublicT] = PublicRun()
    if row is None:
        return response
    response.run_id = row.run_id
    if row.status not in {"running", "completed", "partial_failure", "failed", "skipped"}:
        response.projection_status = "invalid"
        return response
    response.status = "running" if row.status == "running" else AgentOutputStatus(row.status)
    response.error_code = _safe_code(row.error_code)
    if row.status == "running" or row.output_json is None:
        return response
    try:
        if hashlib.sha256(row.output_json.encode()).hexdigest() != row.output_sha256:
            raise ValueError("output_hash_mismatch")
        output = output_type.model_validate_json(row.output_json)
        if (
            output.analysis_id != row.analysis_id
            or output.run_id != row.run_id
            or output.agent_name != row.agent_name
            or output.status != row.status
        ):
            raise ValueError("output_identity_mismatch")
        result = project(output.result) if output.result is not None else None
    except (ValidationError, ValueError, TypeError):
        response.projection_status = "invalid"
        return response
    response.result = result
    response.projection_status = "available" if result is not None else "unavailable"
    response.error_code = _safe_code(output.error.code) if output.error else None
    response.warning_codes = tuple(
        code for item in output.warnings if (code := _safe_code(item.code)) is not None
    )
    response.token_usage = output.execution_metadata.usage.token_usage
    return response


def project_admet(result: AdmetAgentResult) -> AdmetSummary:
    context = result.context
    return AdmetSummary(
        toxicity_axes=result.toxicity_axes,
        source_run_id=context.source_run_id,
        source_tool_call_id=context.source_tool_call_id,
        tool_version=context.tool_version,
        reference_population=context.reference_population,
        selected_endpoint_count=len(context.rows),
        catalog_endpoint_count=context.catalog_endpoint_count,
        selection_is_subset=context.is_partial,
        missing_endpoints=result.missing_endpoints,
        endpoints=tuple(AdmetEndpointSummary.model_validate(row._asdict()) for row in context.rows),
        interpretation=result.interpretation,
        adme=AdmetDomainSummary.model_validate(result.adme.model_dump()) if result.adme else None,
        toxicity=AdmetDomainSummary.model_validate(result.toxicity.model_dump())
        if result.toxicity
        else None,
        interpretation_note=context.interpretation_note,
        limitations=context.limitations,
    )


def project_cardiac_recall(result: AdmetRecallResult) -> CardiacRecallSummary:
    return CardiacRecallSummary(
        source_tool_call_id=result.source_tool_call_id,
        tool_call_id=result.tool_call_id,
        tool_version=result.tool_version,
        execution_mode=result.execution_mode,
        predictions=tuple(
            CardiacChannelSummary.model_validate(item.model_dump(mode="json"))
            for item in result.predictions
        ),
        limitations=result.limitations,
    )


def project_dta(result: DtaAgentResult) -> DtaSummary:
    return DtaSummary(
        assay_policy_version=result.assay_policy_version,
        source_target_run_id=result.source_target_run_id,
        candidates=tuple(
            DtaCandidateSummary.model_validate(item.model_dump()) for item in result.candidates
        ),
        interpretation=result.interpretation,
        interpretation_note=result.interpretation_note,
    )


def project_decision(result: DecisionResult) -> DecisionSummary | None:
    if result.assessment.decision_phase != "final":
        return None
    return DecisionSummary(
        **result.assessment.model_dump(exclude={"decision_phase", "recall_request"}),
        server_metadata=result.server_metadata,
        context_version=result.context_version,
        source_run_ids=result.source_run_ids,
        missing_stages=tuple(result.missing_stages),
        scope=result.scope,
    )


def _stored_input(row: AgentRunRecord) -> AgentInput | None:
    """해시와 식별자가 일치하는 입력에서만 요청 관계를 읽는다."""
    if (
        row.input_json is None
        or hashlib.sha256(row.input_json.encode()).hexdigest() != row.input_sha256
    ):
        return None
    try:
        saved = AgentInput.model_validate_json(row.input_json)
    except ValidationError:
        return None
    if (
        saved.analysis_id != row.analysis_id
        or saved.run_id != row.run_id
        or saved.agent_name != row.agent_name
        or saved.attempt != row.attempt
    ):
        return None
    return saved


def _decision_request(row: AgentRunRecord) -> DecisionRecallRequest | None:
    if (
        row.output_json is None
        or hashlib.sha256(row.output_json.encode()).hexdigest() != row.output_sha256
    ):
        return None
    try:
        saved = AgentOutput[DecisionResult].model_validate_json(row.output_json)
    except ValidationError:
        return None
    if (
        saved.analysis_id != row.analysis_id
        or saved.run_id != row.run_id
        or saved.agent_name != AnalysisStageName.DECISION
        or saved.status != row.status
        or saved.result is None
    ):
        return None
    return saved.result.assessment.recall_request


def _request_summary(
    requesting_run_id: UUID, request: DecisionRecallRequest
) -> AgentCallRequestSummary:
    return AgentCallRequestSummary(
        requesting_run_id=requesting_run_id,
        gap_kind=request.gap_kind.value,
        objective=request.objective,
        reason_code=request.reason_code,
        required_endpoints=request.required_endpoints,
    )


def project_agent_calls(rows: tuple[AgentRunRecord, ...]) -> tuple[AgentCallSummary, ...]:
    """저장된 Agent attempt를 순서형 호출로 투영하고 검증된 요청 연결만 노출한다."""
    by_id = {row.run_id: row for row in rows}
    responses: dict[UUID, UUID] = {}
    for row in rows:
        if row.agent_name != AnalysisStageName.ADMET or row.attempt <= 1:
            continue
        parent = by_id.get(row.parent_run_id) if row.parent_run_id else None
        saved = _stored_input(row)
        if (
            parent is not None
            and parent.agent_name == AnalysisStageName.DECISION
            and saved is not None
            and saved.recall_request is not None
            and _decision_request(parent) == saved.recall_request
        ):
            responses[parent.run_id] = row.run_id
    ordered = sorted(
        rows, key=lambda row: (list(AnalysisStageName).index(row.agent_name), row.attempt)
    )
    calls: list[AgentCallSummary] = []
    for row in ordered:
        saved_input = _stored_input(row)
        purpose: Literal["initial", "evidence_followup", "reassessment", "unverified"] = (
            "initial" if row.attempt == 1 else "unverified"
        )
        trigger = None
        responds_to = None
        request = None
        result_kind: Literal[
            "target_reference", "admet_baseline", "cardiac_ion_channel", "dta", "decision"
        ] = "target_reference"
        projection: (
            PublicRun[AdmetSummary]
            | PublicRun[CardiacRecallSummary]
            | PublicRun[DtaSummary]
            | PublicRun[DecisionSummary]
            | None
        ) = None
        if row.agent_name == AnalysisStageName.ADMET:
            if row.attempt == 1:
                result_kind = "admet_baseline"
                projection = project_run(row, AgentOutput[AdmetAgentResult], project_admet)
            elif saved_input is not None and saved_input.recall_request is not None:
                result_kind = "cardiac_ion_channel"
                if row.parent_run_id is not None and responses.get(row.parent_run_id) == row.run_id:
                    purpose = "evidence_followup"
                    trigger = responds_to = row.parent_run_id
                    request = _request_summary(row.parent_run_id, saved_input.recall_request)
                projection = project_run(
                    row, AgentOutput[AdmetRecallResult], project_cardiac_recall
                )
        elif row.agent_name == AnalysisStageName.DTA:
            result_kind = "dta"
            projection = project_run(row, AgentOutput[DtaAgentResult], project_dta)
        elif row.agent_name == AnalysisStageName.DECISION:
            result_kind = "decision"
            projection = project_run(row, AgentOutput[DecisionResult], project_decision)
            if row.attempt == 1:
                issued = _decision_request(row)
                if issued is not None:
                    request = _request_summary(row.run_id, issued)
            elif saved_input is not None and saved_input.recall_feedback is not None:
                if (
                    row.parent_run_id in responses
                    and responses[row.parent_run_id] == saved_input.recall_feedback.run_id
                    and _decision_request(by_id[row.parent_run_id])
                    == saved_input.recall_feedback.request
                ):
                    purpose = "reassessment"
                    trigger = row.parent_run_id
                    responds_to = saved_input.recall_feedback.run_id
                    request = _request_summary(
                        row.parent_run_id, saved_input.recall_feedback.request
                    )
        if projection is None:
            status: AgentOutputStatus | Literal["running"] | None = (
                "running"
                if row.status == "running"
                else AgentOutputStatus(row.status)
                if row.status in AgentOutputStatus._value2member_map_
                else None
            )
            projection_status: Literal["available", "unavailable", "invalid", "not_projected"] = (
                "not_projected" if result_kind == "target_reference" else "unavailable"
            )
            result: AdmetSummary | CardiacRecallSummary | DtaSummary | DecisionSummary | None = None
            error_code = _safe_code(row.error_code)
            warning_codes: tuple[str, ...] = ()
            token_usage = None
        else:
            status = projection.status
            projection_status = projection.projection_status
            result = projection.result
            error_code = projection.error_code
            warning_codes = projection.warning_codes
            token_usage = projection.token_usage
        calls.append(
            AgentCallSummary(
                agent_name=row.agent_name,
                call_number=row.attempt,
                run_id=row.run_id,
                status=status,
                purpose=purpose,
                triggering_run_id=trigger,
                responds_to_run_id=responds_to,
                request=request,
                response_run_id=responses.get(row.run_id),
                result_kind=result_kind,
                projection_status=projection_status,
                result=result,
                error_code=error_code,
                warning_codes=warning_codes,
                token_usage=token_usage,
            )
        )
    return tuple(calls)


async def read_specialist_results(
    analysis_id: UUID,
    repository: AnalysisRepository,
    session_token: str,
    visitor_id: str | None = None,
) -> SpecialistResultsResponse:
    """소유권 확인 후 최신 전문 run 세 개만 읽는다. provider/LLM 호출·SQL 쓰기 없음."""
    record = await read_owned_analysis(analysis_id, repository, session_token, visitor_id)
    return SpecialistResultsResponse(
        analysis_id=record.id,
        status=record.status,
        admet=project_run(
            await repository.get_run_attempt(analysis_id, AnalysisStageName.ADMET, 1),
            AgentOutput[AdmetAgentResult],
            project_admet,
        ),
        dta=project_run(
            await repository.get_latest_run(analysis_id, AnalysisStageName.DTA),
            AgentOutput[DtaAgentResult],
            project_dta,
        ),
        decision=project_run(
            await repository.get_latest_run(analysis_id, AnalysisStageName.DECISION),
            AgentOutput[DecisionResult],
            project_decision,
        ),
        calls=project_agent_calls(await repository.list_agent_calls(analysis_id)),
    )
