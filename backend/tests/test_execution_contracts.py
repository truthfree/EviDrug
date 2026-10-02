"""Agent와 도구 실행 계약의 상태 및 참조 무결성 검증."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from evidrug_api.analysis_input.models import AnalysisInputResponse, TargetMode
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.decision.recall import DecisionRecallRequest, EvidenceGapKind
from evidrug_api.execution_contracts import (
    AgentInput,
    AgentOutput,
    AgentOutputStatus,
    ArtifactReference,
    EvidenceClaim,
    EvidenceDirection,
    ExecutionError,
    ExecutionLimits,
    ExecutionMetadata,
    FollowupRequest,
    ToolAdmission,
    ToolAdmissionDecision,
    ToolObservation,
    ToolObservationStatus,
    ToolRequest,
)


class AdmetArguments(BaseModel):
    """ADMET tool의 예시 전용 인수."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_smiles: str


class AdmetResult(BaseModel):
    """ADMET tool과 Agent의 예시 전용 결과."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ames_probability: float


def execution_metadata() -> ExecutionMetadata:
    """결정적인 시간값을 가진 실행 메타데이터를 만든다."""

    started_at = datetime(2026, 9, 18, tzinfo=UTC)
    return ExecutionMetadata(
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=25),
        duration_ms=25,
        implementation_version="test-v1",
    )


def test_agent_input_preserves_validated_case_and_limits() -> None:
    """Agent 입력은 확정된 case와 실행 한도를 함께 직렬화한다."""

    run_id = uuid4()
    agent_input = AgentInput(
        analysis_id=uuid4(),
        run_id=run_id,
        agent_name=AnalysisStageName.ADMET,
        attempt=1,
        case_input=AnalysisInputResponse(
            disease_id="MONDO:0005148",
            disease_name="type 2 diabetes mellitus",
            target_mode=TargetMode.DISCOVER,
            target_name=None,
            original_smiles="CCO",
            canonical_smiles="CCO",
        ),
        execution_limits=ExecutionLimits(
            timeout_seconds=300,
            max_tool_calls=2,
            max_recall_depth=1,
            max_total_tokens=4_000,
            reserved_finalization_tokens=500,
        ),
    )

    payload = agent_input.model_dump(mode="json")

    assert payload["schema_version"] == "1"
    assert payload["run_id"] == str(run_id)
    assert payload["case_input"]["canonical_smiles"] == "CCO"
    assert payload["execution_limits"]["max_tool_calls"] == 2
    payload["recall_request"] = DecisionRecallRequest(
        gap_kind=EvidenceGapKind.CARDIAC_ION_CHANNEL,
        objective="심장 이온채널 근거 확인",
        reason_code="cardiac_evidence_gap",
    ).model_dump(mode="json")
    with pytest.raises(ValidationError, match="second agent call"):
        AgentInput.model_validate(payload)


def test_tool_request_validates_agent_specific_arguments() -> None:
    """generic arguments schema 밖의 입력은 공통 envelope에서도 거부한다."""

    with pytest.raises(ValidationError, match="extra_forbidden"):
        ToolRequest[AdmetArguments].model_validate(
            {
                "request_id": uuid4(),
                "run_id": uuid4(),
                "tool_id": "admet_ai",
                "tool_version": "1.4.0",
                "arguments": {"canonical_smiles": "CCO", "api_key": "must-not-pass"},
                "objective": "독성 endpoint를 예측한다.",
            }
        )


def test_successful_tool_observation_requires_approved_result() -> None:
    """성공 관측은 승인 기록과 타입 지정 결과를 함께 가져야 한다."""

    raw_result = ArtifactReference(
        artifact_id=uuid4(),
        schema_name="admet_ai.raw",
        schema_version="1.4.0",
        media_type="application/json",
        sha256="a" * 64,
    )
    observation = ToolObservation[AdmetResult](
        tool_call_id=uuid4(),
        request_id=uuid4(),
        run_id=uuid4(),
        status=ToolObservationStatus.SUCCEEDED,
        admission=ToolAdmission(
            decision=ToolAdmissionDecision.APPROVED,
            policy_version="tool-policy-v1",
        ),
        result=AdmetResult(ames_probability=0.12),
        raw_result=raw_result,
        execution_metadata=execution_metadata(),
    )

    assert observation.result == AdmetResult(ames_probability=0.12)
    assert observation.raw_result == raw_result
    assert "raw_result" not in observation.result.model_dump()
    assert observation.error is None


def test_rejected_tool_observation_requires_reason_and_error() -> None:
    """실행 전 거부는 고정 reason code와 공개 가능한 오류를 보존한다."""

    with pytest.raises(ValidationError, match="rejected admission requires reason_code"):
        ToolAdmission(
            decision=ToolAdmissionDecision.REJECTED,
            policy_version="tool-policy-v1",
        )

    observation = ToolObservation[AdmetResult](
        tool_call_id=uuid4(),
        request_id=uuid4(),
        run_id=uuid4(),
        status=ToolObservationStatus.REJECTED,
        admission=ToolAdmission(
            decision=ToolAdmissionDecision.REJECTED,
            policy_version="tool-policy-v1",
            reason_code="tool_budget_exhausted",
        ),
        error=ExecutionError(
            code="tool_budget_exhausted",
            message="이 run의 도구 호출 예산을 모두 사용했습니다.",
            retryable=False,
        ),
        execution_metadata=execution_metadata(),
    )

    assert observation.status is ToolObservationStatus.REJECTED


def test_completed_agent_output_requires_result() -> None:
    """완료 상태가 비어 있는 결과로 저장되는 것을 막는다."""

    with pytest.raises(ValidationError, match="require a result"):
        AgentOutput[AdmetResult](
            analysis_id=uuid4(),
            run_id=uuid4(),
            agent_name=AnalysisStageName.ADMET,
            status=AgentOutputStatus.COMPLETED,
            execution_metadata=execution_metadata(),
        )


def test_partial_failure_preserves_result_and_error() -> None:
    """부분 실패는 사용 가능한 결과와 실패 원인을 모두 남긴다."""

    output = AgentOutput[AdmetResult](
        analysis_id=uuid4(),
        run_id=uuid4(),
        agent_name=AnalysisStageName.ADMET,
        status=AgentOutputStatus.PARTIAL_FAILURE,
        result=AdmetResult(ames_probability=0.12),
        error=ExecutionError(
            code="auxiliary_tool_unavailable",
            message="보조 근거 조회에 실패했습니다.",
            retryable=True,
        ),
        execution_metadata=execution_metadata(),
    )

    assert output.result is not None
    assert output.error is not None


def test_schema_version_is_closed() -> None:
    """지원하지 않는 계약 version을 조용히 수용하지 않는다."""

    with pytest.raises(ValidationError, match="literal_error"):
        AgentInput.model_validate(
            {
                "schema_version": "2",
                "analysis_id": uuid4(),
                "run_id": uuid4(),
                "agent_name": "admet",
                "attempt": 1,
                "case_input": {
                    "disease_id": "MONDO:0005148",
                    "disease_name": "type 2 diabetes mellitus",
                    "target_mode": "discover",
                    "target_name": None,
                    "original_smiles": "CCO",
                    "canonical_smiles": "CCO",
                },
                "execution_limits": {
                    "timeout_seconds": 300,
                    "max_tool_calls": 2,
                    "max_recall_depth": 1,
                },
            }
        )


def test_agent_output_validates_claim_and_followup_references() -> None:
    """follow-up은 같은 출력에 포함된 근거와 공백만 참조한다."""

    run_id = uuid4()
    claim_id = uuid4()
    output = AgentOutput[AdmetResult](
        analysis_id=uuid4(),
        run_id=run_id,
        agent_name=AnalysisStageName.ADMET,
        status=AgentOutputStatus.COMPLETED,
        result=AdmetResult(ames_probability=0.12),
        evidence_claims=(
            EvidenceClaim(
                claim_id=claim_id,
                claim_type="admet_risk",
                statement="AMES 변이원성 위험 예측값이 낮다.",
                direction=EvidenceDirection.SUPPORTS,
                value=0.12,
                unit="probability",
                source="admet_ai:AMES",
                source_record_id="AMES",
                retrieved_at=datetime(2026, 9, 18, tzinfo=UTC),
                producer_run_id=run_id,
            ),
        ),
        requested_followups=(
            FollowupRequest(
                request_id=uuid4(),
                target_agent=AnalysisStageName.ADMET,
                objective="간독성 근거의 적용 범위를 재검토한다.",
                required_fields=("applicability_domain",),
                related_claim_ids=(claim_id,),
            ),
        ),
        execution_metadata=execution_metadata(),
    )

    assert output.evidence_claims[0].producer_run_id == run_id
    assert output.requested_followups[0].related_claim_ids == (claim_id,)


def test_agent_output_rejects_foreign_claim_reference() -> None:
    """출력에 없는 claim ID를 follow-up에 넣을 수 없다."""

    with pytest.raises(ValidationError, match="must reference included evidence claims"):
        AgentOutput[AdmetResult](
            analysis_id=uuid4(),
            run_id=uuid4(),
            agent_name=AnalysisStageName.DECISION,
            status=AgentOutputStatus.COMPLETED,
            result=AdmetResult(ames_probability=0.12),
            requested_followups=(
                FollowupRequest(
                    request_id=uuid4(),
                    target_agent=AnalysisStageName.ADMET,
                    objective="간독성 근거를 재검토한다.",
                    required_fields=("hepatotoxicity",),
                    related_claim_ids=(UUID("00000000-0000-0000-0000-000000000001"),),
                ),
            ),
            execution_metadata=execution_metadata(),
        )


def test_artifact_reference_requires_sha256() -> None:
    """원본 결과 참조에는 무결성을 확인할 SHA-256이 필요하다."""

    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        ArtifactReference(
            artifact_id=uuid4(),
            schema_name="admet_ai.raw",
            schema_version="1.4.0",
            media_type="application/json",
            sha256="not-a-sha256",
        )
