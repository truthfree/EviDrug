"""검증된 Target shortlist를 순차 예측하고 후보별 관측·실패를 보존한다."""

from datetime import UTC, datetime
from typing import Literal, Self, TypedDict, cast
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.dta.contracts import DtaArguments, DtaModel, DtaObservation, DtaResult
from evidrug_api.dta.evidence import (
    AssayEvidence,
    AssaySource,
    DtaAssayArguments,
    DtaAssayResult,
    DtaEvidenceAssessment,
    assess_dta_evidence,
)
from evidrug_api.execution_contracts.agent import (
    AgentInput,
    AgentOutput,
    AgentOutputStatus,
    AgentWarning,
)
from evidrug_api.execution_contracts.common import (
    ContractModel,
    ExecutionError,
    ExecutionMetadata,
    ExecutionUsage,
)
from evidrug_api.orchestration.contracts import AgentExecutionResult
from evidrug_api.orchestration.fixed_tools import FixedToolRun
from evidrug_api.orchestration.reasoning import Interpretation, ReasoningOutcome, SpecialistReasoner
from evidrug_api.orchestration.upstream import InvalidUpstream, load_upstream
from evidrug_api.target_hypothesis.contracts import TargetHypothesisResult
from evidrug_api.tool_admission.registry import ToolBinding


class CandidateIdentity(TypedDict):
    ensembl_id: str
    approved_symbol: str
    uniprot_accession: str
    target_sequence_sha256: str


class DtaModelRun(ContractModel):
    """모델별 독립 실행. 같은 score type이어도 서로 합치거나 평균하지 않는다."""

    tool_id: str = Field(min_length=1, max_length=120)
    tool_call_id: UUID | None = None
    status: Literal["succeeded", "failed", "skipped"]
    model: DtaModel | None = None
    observations: tuple[DtaObservation, ...] = ()
    error_code: str | None = None
    duration_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == "succeeded":
            if (
                self.model is None
                or not self.observations
                or self.error_code
                or self.tool_call_id is None
            ):
                raise ValueError(
                    "successful DTA model run requires observations and tool reference"
                )
        elif self.observations or self.model is not None or not self.error_code:
            raise ValueError("failed/skipped DTA model run cannot contain predictions")
        return self


class DtaAssayRun(ContractModel):
    """DTA snapshot의 assay 결과를 정규화 SQL tool call과 연결한다."""

    tool_id: str = Field(min_length=1, max_length=120)
    tool_call_id: UUID
    source: AssaySource
    status: Literal["succeeded", "no_records", "failed"]
    error_code: str | None = Field(default=None, max_length=80)
    external_requests: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("failed assay run requires an error only")
        expected_tool = {
            AssaySource.BINDINGDB: "bindingdb",
            AssaySource.CHEMBL: "chembl",
            AssaySource.PUBCHEM: "pubchem_bioassay",
        }[self.source]
        if self.tool_id != expected_tool:
            raise ValueError("assay tool must match provider source")
        return self


class DtaCandidateResult(ContractModel):
    """서열 원문 대신 검증된 식별자와 hash로 원 도구 결과를 연결한다."""

    ensembl_id: str = Field(pattern=r"^ENSG\d+$")
    approved_symbol: str = Field(min_length=1, max_length=120)
    uniprot_accession: str = Field(pattern=r"^[A-Z0-9]+$")
    target_sequence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_call_id: UUID | None = None
    status: Literal["succeeded", "failed", "skipped"]
    model: DtaModel | None = None
    observations: tuple[DtaObservation, ...] = ()
    error_code: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    # 두 모델 도입 전 저장된 Agent 출력은 이 필드가 없다.
    model_runs: tuple[DtaModelRun, ...] = ()
    experimental_evidence: tuple[AssayEvidence, ...] = ()
    # assay SQL persistence 도입 전 저장된 Agent 출력은 이 필드가 없다.
    assay_runs: tuple[DtaAssayRun, ...] = ()
    evidence_assessment: DtaEvidenceAssessment | None = None
    evidence_errors: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == "succeeded":
            if (
                self.model is None
                or not self.observations
                or self.error_code
                or self.tool_call_id is None
            ):
                raise ValueError(
                    "successful DTA candidate requires model, observations and tool reference"
                )
        elif self.observations or self.model is not None or not self.error_code:
            raise ValueError("failed/skipped candidate cannot contain predictions")
        if len({item.tool_id for item in self.model_runs}) != len(self.model_runs):
            raise ValueError("duplicate DTA model tool")
        if len({item.tool_id for item in self.assay_runs}) != len(self.assay_runs):
            raise ValueError("duplicate DTA assay tool")
        if not self.model_runs:
            return self
        successful = tuple(item for item in self.model_runs if item.status == "succeeded")
        if self.status == "succeeded":
            if not successful or not any(
                item.tool_call_id == self.tool_call_id
                and item.model == self.model
                and item.observations == self.observations
                for item in successful
            ):
                raise ValueError("candidate summary must reference one successful model run")
        elif successful:
            raise ValueError("failed/skipped candidate cannot contain successful model runs")
        return self


class DtaAgentResult(ContractModel):
    """점수 유형과 단위를 섞지 않고 후보별 결과를 그대로 전달한다."""

    source_target_run_id: UUID
    assay_policy_version: str = "legacy"
    candidates: tuple[DtaCandidateResult, ...] = Field(min_length=1, max_length=5)
    interpretation: Interpretation | None = None
    interpretation_note: str = (
        "모델 결합 예측은 실험값·약효·억제/활성화 방향의 증명이 아닙니다. "
        "서로 다른 score type은 합산하지 않습니다."
    )

    @model_validator(mode="after")
    def unique_candidates(self) -> Self:
        if len({item.ensembl_id for item in self.candidates}) != len(self.candidates):
            raise ValueError("duplicate DTA candidate")
        return self


class DtaAgent:
    """LLM 도구 선택 없이 고정 shortlist를 admission을 통해 순차 실행한다."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        binding: ToolBinding | tuple[ToolBinding, ...],
        reasoner: SpecialistReasoner,
        assay_bindings: tuple[ToolBinding, ...] = (),
    ) -> None:
        self.factory = factory
        self.bindings = binding if isinstance(binding, tuple) else (binding,)
        if not self.bindings:
            raise ValueError("DTA Agent requires at least one tool binding")
        self.reasoner = reasoner
        self.assay_bindings = assay_bindings
        sources = {binding.tool_id for binding in assay_bindings}
        if sources and sources != {"pubchem_bioassay"}:
            raise ValueError("DTA assay bindings require PubChem")

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        """입력 소유권·서열 hash 검증 후 후보 한도를 적용하고 부분 실패를 보존한다."""
        started = datetime.now(UTC)
        if agent_input.agent_name != AnalysisStageName.DTA:
            raise InvalidUpstream("dta_agent_mismatch")
        references = agent_input.upstream_outputs
        if len(references) != 1 or references[0].agent_name != AnalysisStageName.TARGET_HYPOTHESIS:
            raise InvalidUpstream("dta_requires_target")
        target = await load_upstream(
            self.factory, agent_input, references[0], AgentOutput[TargetHypothesisResult]
        )
        assert target.result is not None
        if target.result.disease_id != agent_input.case_input.disease_id:
            raise InvalidUpstream("dta_target_disease_mismatch")
        tools = FixedToolRun(self.factory, (*self.bindings, *self.assay_bindings), agent_input)
        await tools.start()
        candidates = []
        calls = 0
        external_requests = 0
        for candidate in (target.result.primary, *target.result.alternatives):
            identity = CandidateIdentity(
                ensembl_id=candidate.ensembl_id,
                approved_symbol=candidate.approved_symbol,
                uniprot_accession=candidate.uniprot_accession,
                target_sequence_sha256=candidate.target_sequence_sha256,
            )
            model_runs: list[DtaModelRun] = []
            for binding in self.bindings:
                if calls >= agent_input.execution_limits.max_tool_calls:
                    model_runs.append(
                        DtaModelRun(
                            tool_id=binding.tool_id,
                            status="skipped",
                            error_code="tool_budget_exhausted",
                        )
                    )
                    continue
                observation = await tools.execute(
                    candidate.ensembl_id + ":" + binding.tool_id,
                    DtaArguments(
                        canonical_smiles=agent_input.case_input.canonical_smiles,
                        target_sequence=candidate.target_sequence,
                    ),
                    "검증된 shortlist 후보의 모델별 분자-단백질 결합 점수 예측",
                    binding=binding,
                )
                calls += observation.execution_metadata.usage.tool_calls
                external_requests += observation.execution_metadata.usage.external_requests
                result = (
                    DtaResult.model_validate(observation.result.model_dump())
                    if observation.result
                    else None
                )
                model_runs.append(
                    DtaModelRun(
                        tool_id=binding.tool_id,
                        tool_call_id=observation.tool_call_id,
                        status="succeeded" if result else "failed",
                        model=result.model if result else None,
                        observations=result.observations if result else (),
                        error_code=observation.error.code if observation.error else None,
                        duration_ms=observation.execution_metadata.duration_ms,
                    )
                )
            successful_runs = tuple(item for item in model_runs if item.status == "succeeded")
            if not successful_runs:
                candidates.append(
                    DtaCandidateResult(
                        **identity,
                        status=(
                            "skipped"
                            if all(item.status == "skipped" for item in model_runs)
                            else "failed"
                        ),
                        error_code=(
                            "tool_budget_exhausted"
                            if all(item.status == "skipped" for item in model_runs)
                            else "dta_models_unavailable"
                        ),
                        model_runs=tuple(model_runs),
                        evidence_assessment=assess_dta_evidence(
                            model_runs,
                            (),
                            agent_input.case_input.potency_criterion,
                        ),
                    )
                )
                continue
            experimental_evidence: list[AssayEvidence] = []
            assay_runs: list[DtaAssayRun] = []
            evidence_errors: list[str] = []
            pubchem_executed = False
            assay_by_id = {binding.tool_id: binding for binding in self.assay_bindings}
            assessment = assess_dta_evidence(
                successful_runs,
                tuple(experimental_evidence),
                agent_input.case_input.potency_criterion,
            )
            pubchem = assay_by_id.get("pubchem_bioassay")
            execution_issue: (
                Literal["provider_disabled", "tool_budget_exhausted", "execution_failed"] | None
            ) = (
                "provider_disabled"
                if assessment.pubchem_requested and pubchem is None
                else "tool_budget_exhausted"
                if assessment.pubchem_requested
                and calls >= agent_input.execution_limits.max_tool_calls
                else None
            )
            if (
                assessment.pubchem_requested
                and pubchem is not None
                and calls < agent_input.execution_limits.max_tool_calls
            ):
                observation = await tools.execute(
                    candidate.ensembl_id + ":pubchem_bioassay",
                    DtaAssayArguments(
                        canonical_smiles=agent_input.case_input.canonical_smiles,
                        uniprot_accession=candidate.uniprot_accession,
                    ),
                    "두 결합 모델의 기준선 판단 불일치에 대한 정량 근거 확인",
                    binding=pubchem,
                )
                calls += observation.execution_metadata.usage.tool_calls
                external_requests += observation.execution_metadata.usage.external_requests
                pubchem_executed = True
                execution_issue = (
                    "execution_failed" if observation.status.value != "succeeded" else None
                )
                if observation.result is not None:
                    assay_result = DtaAssayResult.model_validate(observation.result.model_dump())
                    experimental_evidence.extend(assay_result.evidence)
                    assay_runs.append(
                        DtaAssayRun(
                            tool_id="pubchem_bioassay",
                            tool_call_id=observation.tool_call_id,
                            source=assay_result.source,
                            status=assay_result.status,
                            external_requests=assay_result.external_requests,
                        )
                    )
                elif observation.error is not None:
                    evidence_errors.append(f"pubchem_bioassay:{observation.error.code}")
                    assay_runs.append(
                        DtaAssayRun(
                            tool_id="pubchem_bioassay",
                            tool_call_id=observation.tool_call_id,
                            source=AssaySource.PUBCHEM,
                            status="failed",
                            error_code=observation.error.code,
                            external_requests=observation.execution_metadata.usage.external_requests,
                        )
                    )
            assessment = assess_dta_evidence(
                successful_runs,
                tuple(experimental_evidence),
                agent_input.case_input.potency_criterion,
                pubchem_executed=pubchem_executed,
                pubchem_execution_issue=execution_issue,
            )
            representative = successful_runs[0]
            candidates.append(
                DtaCandidateResult(
                    **identity,
                    tool_call_id=representative.tool_call_id,
                    status="succeeded",
                    model=representative.model,
                    observations=representative.observations,
                    duration_ms=sum(item.duration_ms for item in model_runs),
                    model_runs=tuple(model_runs),
                    experimental_evidence=tuple(experimental_evidence),
                    assay_runs=tuple(assay_runs),
                    evidence_assessment=assessment,
                    evidence_errors=tuple(evidence_errors),
                )
            )
        succeeded = sum(item.status == "succeeded" for item in candidates)
        status = (
            AgentOutputStatus.COMPLETED
            if succeeded == len(candidates)
            and all(
                all(run.status == "succeeded" for run in item.model_runs) for item in candidates
            )
            and all(not item.evidence_errors for item in candidates)
            else AgentOutputStatus.PARTIAL_FAILURE
            if succeeded
            else AgentOutputStatus.FAILED
        )
        interpreted = ReasoningOutcome(None)
        if succeeded:
            evidence: dict[str, object] = {
                item.ensembl_id: item.model_dump(mode="json", exclude={"target_sequence_sha256"})
                for item in candidates
            }
            interpreted = await self.reasoner.interpret(
                "DTA", evidence, agent_input.execution_limits
            )
            if interpreted.error_code:
                status = AgentOutputStatus.PARTIAL_FAILURE
        finished = datetime.now(UTC)
        if interpreted.error_code:
            error_message = (
                "결합 예측은 완료했으나 LLM 해석을 완료하지 못했습니다."
                if succeeded == len(candidates)
                else "일부 후보의 결합 예측이 실패/생략되었으며 LLM 해석도 완료하지 못했습니다."
            )
        elif succeeded == len(candidates):
            error_message = "일부 후보에서 모델 예측이 실패하거나 예산으로 생략되었습니다."
        else:
            error_message = "일부 또는 모든 후보의 결합 예측이 실패하거나 생략되었습니다."
        incomplete_code = (
            "dta_models_incomplete" if succeeded == len(candidates) else "dta_candidates_incomplete"
        )
        reasoning_warnings = []
        if interpreted.diagnostic_code:
            reasoning_warnings.append(
                AgentWarning(
                    code=interpreted.diagnostic_code,
                    message=interpreted.diagnostic_message,
                )
            )
        if interpreted.json_fence_removed:
            reasoning_warnings.append(
                AgentWarning(
                    code="reasoning_json_fence_removed",
                    message=(
                        "단일 JSON 코드블록 포장을 제거한 뒤 내용·근거 ID 검증을 통과했습니다."
                    ),
                )
            )
        output = AgentOutput[DtaAgentResult](
            analysis_id=agent_input.analysis_id,
            run_id=agent_input.run_id,
            agent_name=agent_input.agent_name,
            status=status,
            result=DtaAgentResult(
                assay_policy_version="pubchem-recall-v1",
                source_target_run_id=target.run_id,
                candidates=tuple(candidates),
                interpretation=interpreted.interpretation,
            )
            if succeeded
            else None,
            error=None
            if status == AgentOutputStatus.COMPLETED
            else ExecutionError(
                code=interpreted.error_code or incomplete_code,
                message=error_message,
                retryable=False,
            ),
            warnings=tuple(reasoning_warnings),
            execution_metadata=ExecutionMetadata(
                started_at=started,
                finished_at=finished,
                duration_ms=int((finished - started).total_seconds() * 1000),
                implementation_version="dta-shortlist-agent-v4",
                components=interpreted.components,
                usage=ExecutionUsage(
                    tool_calls=calls,
                    token_usage=interpreted.usage,
                    external_requests=external_requests + interpreted.external_requests,
                ),
            ),
        )
        return AgentExecutionResult(output=cast(AgentOutput[BaseModel], output))
