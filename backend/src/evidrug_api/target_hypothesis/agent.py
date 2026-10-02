"""근거 기반 shortlist와 검증된 DTA 입력을 만드는 Target Hypothesis Agent."""

import hashlib
from datetime import UTC, datetime
from time import perf_counter
from typing import cast
from uuid import uuid4

from pydantic import BaseModel

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.execution_contracts.agent import (
    AgentInput,
    AgentOutput,
    AgentOutputStatus,
    AgentWarning,
    EvidenceClaim,
    EvidenceDirection,
    EvidenceGap,
)
from evidrug_api.execution_contracts.common import (
    ComponentVersion,
    ExecutionError,
    ExecutionMetadata,
    ExecutionUsage,
    ProviderConfidence,
    TokenUsage,
)
from evidrug_api.openai_gateway import ResponseUsage
from evidrug_api.openai_gateway.diagnostics import model_call_diagnostic
from evidrug_api.orchestration.contracts import AgentExecutionResult, AgentExecutorUnavailable
from evidrug_api.reader_numbers import format_reader_number
from evidrug_api.target_hypothesis.causal_policy import CausalSupportPolicy
from evidrug_api.target_hypothesis.contracts import (
    AssessedTargetCandidate,
    CausalEvidenceAxis,
    CausalSupportStatus,
    ExcludedTargetCandidate,
    TargetCandidate,
    TargetEligibility,
    TargetHypothesisResult,
    TargetRankingItem,
    TargetRecommendation,
    VerifiedProtein,
)
from evidrug_api.target_hypothesis.policy import SmallMoleculePrioritizationPolicy
from evidrug_api.target_hypothesis.providers import (
    PharosProvider,
    PharosUnavailable,
    ProteinNotVerified,
    ProteinProvider,
    ProteinUnavailable,
    TargetCandidateProvider,
    TargetCandidatesNotFound,
    TargetCandidatesUnavailable,
)
from evidrug_api.target_hypothesis.reasoner import (
    PROMPT_VERSION,
    InvalidTargetSelection,
    TargetReasoner,
    TargetReasoningUnavailable,
)

IMPLEMENTATION_VERSION = "target-causal-support-agent-v2"


class TargetHypothesisAgent:
    """association과 tractability를 분리해 검증 가능한 후보 목록을 만든다."""

    def __init__(
        self,
        candidate_provider: TargetCandidateProvider,
        protein_provider: ProteinProvider,
        reasoner: TargetReasoner,
        prioritization_policy: SmallMoleculePrioritizationPolicy,
        *,
        candidate_limit: int,
        shortlist_limit: int,
        causal_support_policy: CausalSupportPolicy | None = None,
        pharos_provider: PharosProvider | None = None,
    ) -> None:
        self._candidate_provider = candidate_provider
        self._protein_provider = protein_provider
        self._reasoner = reasoner
        self._prioritization_policy = prioritization_policy
        self._candidate_limit = candidate_limit
        self._shortlist_limit = min(candidate_limit, shortlist_limit)
        self._causal_support_policy = causal_support_policy or CausalSupportPolicy()
        self._pharos_provider = pharos_provider

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        """표적 근거를 조회하고 검증된 human protein shortlist를 DTA에 전달한다."""
        if agent_input.agent_name is not AnalysisStageName.TARGET_HYPOTHESIS:
            raise AgentExecutorUnavailable(
                f"{agent_input.agent_name.value} executor is not configured"
            )

        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        external_requests = 0
        try:
            external_requests += 1
            candidates = await self._candidate_provider.find_candidates(
                disease_id=agent_input.case_input.disease_id,
                target_name=agent_input.case_input.target_name,
                limit=self._candidate_limit,
            )
            external_requests += candidates.external_requests - 1
            if self._pharos_provider is not None:
                enriched_candidates = []
                for candidate in candidates.candidates:
                    external_requests += 1
                    evidence = await self._pharos_provider.get_target_evidence(
                        candidate.uniprot_accession
                    )
                    enriched_candidates.append(
                        candidate.model_copy(update={"pharos_evidence": evidence})
                    )
                candidates = candidates.model_copy(
                    update={"candidates": tuple(enriched_candidates)}
                )
            tractability_assessed = self._prioritization_policy.assess_all(candidates.candidates)
            assessed = tuple(
                candidate.model_copy(
                    update={
                        "causal_support": self._causal_support_policy.assess(
                            candidate.candidate.causal_evidence
                        )
                    }
                )
                for candidate in tractability_assessed
            )
            external_requests += 1
            reasoned = await self._reasoner.rank(
                agent_input.case_input,
                assessed,
                shortlist_limit=self._shortlist_limit,
            )
        except TargetCandidatesNotFound:
            return self._failure(
                agent_input,
                started_at,
                started_clock,
                external_requests,
                code="target_not_found",
                message="검증 가능한 질환-표적 후보를 찾지 못했습니다.",
                retryable=False,
            )
        except TargetCandidatesUnavailable:
            return self._failure(
                agent_input,
                started_at,
                started_clock,
                external_requests,
                code="target_evidence_unavailable",
                message="표적 근거 제공자를 사용할 수 없습니다.",
                retryable=True,
            )
        except PharosUnavailable:
            return self._failure(
                agent_input,
                started_at,
                started_clock,
                external_requests,
                code="pharos_unavailable",
                message="Pharos 표적 성숙도 제공자를 사용할 수 없습니다.",
                retryable=True,
            )
        except TargetReasoningUnavailable as error:
            return self._failure(
                agent_input,
                started_at,
                started_clock,
                external_requests,
                code="target_model_unavailable",
                message="표적 후보 우선순위 모델을 사용할 수 없습니다.",
                retryable=True,
                diagnostic_code=model_call_diagnostic(error.__cause__ or error),
            )
        except InvalidTargetSelection as error:
            return self._failure(
                agent_input,
                started_at,
                started_clock,
                external_requests,
                code="invalid_target_selection",
                message="표적 후보 우선순위 결과가 검증 계약을 충족하지 못했습니다.",
                retryable=False,
                diagnostic_code=error.diagnostic_code,
                model=error.generated.model if error.generated else None,
                token_usage=error.generated.usage if error.generated else None,
            )

        assessed_by_id = {candidate.candidate.ensembl_id: candidate for candidate in assessed}
        recommendations: list[TargetRecommendation] = []
        sequence_exclusions: list[ExcludedTargetCandidate] = []
        proteins: list[VerifiedProtein] = []
        for ranking_item in reasoned.ranking.ranked_candidates:
            assessed_candidate = assessed_by_id[ranking_item.ensembl_id]
            external_requests += 1
            try:
                protein = await self._protein_provider.get_reviewed_human_protein(
                    assessed_candidate.candidate.uniprot_accession
                )
            except ProteinNotVerified:
                sequence_exclusions.append(
                    self._exclude(assessed_candidate.candidate, "target_sequence_not_verified")
                )
                continue
            except ProteinUnavailable:
                return self._failure(
                    agent_input,
                    started_at,
                    started_clock,
                    external_requests,
                    code="target_sequence_unavailable",
                    message="표적 단백질 서열 제공자를 사용할 수 없습니다.",
                    retryable=True,
                )
            proteins.append(protein)
            recommendations.append(
                self._recommendation(
                    len(recommendations) + 1,
                    assessed_candidate,
                    ranking_item,
                    protein,
                )
            )

        if not recommendations:
            return self._failure(
                agent_input,
                started_at,
                started_clock,
                external_requests,
                code="target_sequence_not_verified",
                message="reviewed human 단백질 서열을 검증하지 못했습니다.",
                retryable=False,
            )

        ranked_ids = {candidate.ensembl_id for candidate in reasoned.ranking.ranked_candidates}
        omitted = tuple(
            self._exclude(candidate, "not_shortlisted")
            for candidate in candidates.candidates
            if candidate.ensembl_id not in ranked_ids
        )
        excluded = (
            *candidates.excluded_candidates,
            *omitted,
            *sequence_exclusions,
        )
        result = TargetHypothesisResult(
            target_mode=agent_input.case_input.target_mode,
            disease_id=candidates.disease_id,
            disease_name=candidates.disease_name,
            modality=self._prioritization_policy.modality,
            primary=recommendations[0],
            alternatives=tuple(recommendations[1:]),
            excluded_candidates=excluded,
            source_version=candidates.source_version,
            source_retrieved_at=max(
                candidates.retrieved_at,
                *(protein.retrieved_at for protein in proteins),
            ),
        )
        output = AgentOutput[TargetHypothesisResult](
            analysis_id=agent_input.analysis_id,
            run_id=agent_input.run_id,
            agent_name=agent_input.agent_name,
            status=AgentOutputStatus.COMPLETED,
            result=result,
            evidence_claims=self._evidence_claims(
                agent_input,
                recommendations,
                candidates.source_version,
                candidates.retrieved_at,
                proteins,
            ),
            gaps=self._evidence_gaps(recommendations),
            warnings=self._warnings(recommendations, bool(sequence_exclusions)),
            execution_metadata=self._metadata(
                started_at,
                started_clock,
                external_requests,
                source_version=candidates.source_version,
                model=reasoned.generated.model,
                token_usage=reasoned.generated.usage,
            ),
        )
        return AgentExecutionResult(
            output=cast(AgentOutput[BaseModel], output),
            provides_dta_input=True,
        )

    @staticmethod
    def _recommendation(
        rank: int,
        assessed: AssessedTargetCandidate,
        ranking: TargetRankingItem,
        protein: VerifiedProtein,
    ) -> TargetRecommendation:
        candidate = assessed.candidate
        return TargetRecommendation(
            rank=rank,
            ensembl_id=candidate.ensembl_id,
            approved_symbol=candidate.approved_symbol,
            approved_name=candidate.approved_name,
            uniprot_accession=protein.accession,
            protein_name=protein.protein_name,
            target_sequence=protein.sequence,
            target_sequence_sha256=hashlib.sha256(protein.sequence.encode("ascii")).hexdigest(),
            association_score=candidate.association_score,
            data_type_scores=candidate.data_type_scores,
            modality=assessed.modality,
            eligibility=assessed.eligibility,
            eligibility_reason_codes=assessed.reason_codes,
            tractability_assessments=candidate.tractability_assessments,
            pharos_evidence=candidate.pharos_evidence,
            causal_support=assessed.causal_support,
            causal_evidence_ids=ranking.causal_evidence_ids,
            modulation_action=ranking.modulation_action,
            prioritization_rationale=ranking.rationale,
            causal_rationale=ranking.causal_rationale,
        )

    @staticmethod
    def _exclude(candidate: TargetCandidate, reason_code: str) -> ExcludedTargetCandidate:
        return ExcludedTargetCandidate(
            ensembl_id=candidate.ensembl_id,
            approved_symbol=candidate.approved_symbol,
            association_score=candidate.association_score,
            reason_code=reason_code,
        )

    @staticmethod
    def _evidence_claims(
        agent_input: AgentInput,
        recommendations: list[TargetRecommendation],
        source_version: str,
        association_retrieved_at: datetime,
        proteins: list[VerifiedProtein],
    ) -> tuple[EvidenceClaim, ...]:
        claims: list[EvidenceClaim] = []
        for recommendation, protein in zip(recommendations, proteins, strict=True):
            source_record_id = f"{agent_input.case_input.disease_id}:{recommendation.ensembl_id}"
            claims.append(
                EvidenceClaim(
                    claim_id=uuid4(),
                    claim_type="target_disease_association",
                    statement=(
                        f"{recommendation.approved_symbol}의 Open Targets 질환 연관 점수는 "
                        f"{format_reader_number(recommendation.association_score)}입니다."
                    ),
                    direction=EvidenceDirection.SUPPORTS,
                    value=recommendation.association_score,
                    unit="Open Targets association score",
                    confidence=ProviderConfidence(
                        value=recommendation.association_score,
                        scale="Open Targets association score 0-1",
                        method="Open Targets weighted harmonic aggregation; ranking heuristic",
                    ),
                    source=f"Open Targets Platform API {source_version}",
                    source_record_id=source_record_id,
                    retrieved_at=association_retrieved_at,
                    producer_run_id=agent_input.run_id,
                )
            )
            if recommendation.pharos_evidence is not None:
                pharos = recommendation.pharos_evidence
                claims.append(
                    EvidenceClaim(
                        claim_id=uuid4(),
                        claim_type="target_maturity",
                        statement=(
                            f"{recommendation.approved_symbol}의 Pharos Target Development "
                            f"Level은 {pharos.development_level.value}이며 알려진 ligand "
                            f"{pharos.ligand_count}개와 publication {pharos.publication_count}건이 "
                            "집계되어 있습니다."
                        ),
                        direction=EvidenceDirection.UNCERTAIN,
                        value=float(pharos.ligand_count),
                        unit="Pharos ligand records",
                        source=f"Pharos GraphQL API {pharos.source_version}",
                        source_record_id=pharos.uniprot_accession,
                        retrieved_at=pharos.retrieved_at,
                        producer_run_id=agent_input.run_id,
                    )
                )
            causal = recommendation.causal_support
            causal_sources = sorted(
                {
                    item.datasource_id
                    for item in causal.evidence
                    if item.axis is not CausalEvidenceAxis.CLINICAL_VALIDATION
                }
            )
            claims.append(
                EvidenceClaim(
                    claim_id=uuid4(),
                    claim_type="target_causal_support",
                    statement=(
                        f"{recommendation.approved_symbol}의 생물학적 인과 근거 상태는 "
                        f"{causal.status.value}이며 근거 출처는 "
                        + (", ".join(causal_sources) if causal_sources else "확인되지 않음")
                        + "입니다."
                    ),
                    direction=(
                        EvidenceDirection.SUPPORTS
                        if causal.status is CausalSupportStatus.SUPPORTED
                        else EvidenceDirection.UNCERTAIN
                    ),
                    value=float(causal.biological_evidence_count),
                    unit="individual biological causal evidence records",
                    source=f"Open Targets Platform API {source_version}",
                    source_record_id=source_record_id,
                    retrieved_at=association_retrieved_at,
                    producer_run_id=agent_input.run_id,
                )
            )
            if causal.clinical_validation_count:
                clinical_sources = sorted(
                    {
                        item.datasource_id
                        for item in causal.evidence
                        if item.axis is CausalEvidenceAxis.CLINICAL_VALIDATION
                    }
                )
                claims.append(
                    EvidenceClaim(
                        claim_id=uuid4(),
                        claim_type="target_clinical_validation",
                        statement=(
                            f"{recommendation.approved_symbol}의 임상 선행 근거 "
                            f"{causal.clinical_validation_count}건을 "
                            f"{', '.join(clinical_sources)}에서 확인했습니다."
                        ),
                        direction=EvidenceDirection.SUPPORTS,
                        value=float(causal.clinical_validation_count),
                        unit="individual clinical validation evidence records",
                        source=f"Open Targets Platform API {source_version}",
                        source_record_id=source_record_id,
                        retrieved_at=association_retrieved_at,
                        producer_run_id=agent_input.run_id,
                    )
                )
            positive = tuple(
                item.label for item in recommendation.tractability_assessments if item.value
            )
            claims.append(
                EvidenceClaim(
                    claim_id=uuid4(),
                    claim_type="small_molecule_tractability",
                    statement=(
                        f"{recommendation.approved_symbol}의 소분자 tractability 양성 항목: "
                        + (", ".join(positive) if positive else "확인되지 않음")
                    ),
                    direction=(
                        EvidenceDirection.SUPPORTS if positive else EvidenceDirection.UNCERTAIN
                    ),
                    value=float(len(positive)),
                    unit="positive Open Targets tractability assessments",
                    source=f"Open Targets Platform API {source_version}",
                    source_record_id=recommendation.ensembl_id,
                    retrieved_at=association_retrieved_at,
                    producer_run_id=agent_input.run_id,
                )
            )
            claims.append(
                EvidenceClaim(
                    claim_id=uuid4(),
                    claim_type="reviewed_protein_sequence",
                    statement=(
                        f"UniProtKB/Swiss-Prot {recommendation.uniprot_accession}에서 "
                        f"{len(protein.sequence)}개 아미노산의 human protein sequence를 "
                        "확인했습니다."
                    ),
                    direction=EvidenceDirection.SUPPORTS,
                    value=float(len(protein.sequence)),
                    unit="amino acids",
                    source="UniProtKB/Swiss-Prot REST API",
                    source_record_id=recommendation.uniprot_accession,
                    retrieved_at=protein.retrieved_at,
                    producer_run_id=agent_input.run_id,
                )
            )
        return tuple(claims)

    def _evidence_gaps(
        self,
        recommendations: list[TargetRecommendation],
    ) -> tuple[EvidenceGap, ...]:
        gaps = [
            EvidenceGap(
                gap_id=uuid4(),
                description=(
                    f"{candidate.approved_symbol}의 small-molecule tractability 양성 근거가 "
                    "현재 Open Targets 평가에서 확인되지 않았습니다."
                ),
                required_fields=("small_molecule_tractability",),
            )
            for candidate in recommendations
            if candidate.eligibility is TargetEligibility.EXPLORATORY
        ]
        gaps.extend(
            EvidenceGap(
                gap_id=uuid4(),
                description=(
                    f"{candidate.approved_symbol}의 생물학적 인과 근거가 현재 "
                    "Open Targets 개별 evidence에서 확인되지 않았습니다."
                ),
                required_fields=("causal_support",),
            )
            for candidate in recommendations
            if candidate.causal_support.status is CausalSupportStatus.UNKNOWN
        )
        if self._pharos_provider is not None:
            gaps.extend(
                EvidenceGap(
                    gap_id=uuid4(),
                    description=(
                        f"{candidate.approved_symbol}의 Pharos 표적 성숙도 관측을 "
                        "UniProt accession으로 찾지 못했습니다."
                    ),
                    required_fields=("target_maturity",),
                )
                for candidate in recommendations
                if candidate.pharos_evidence is None
            )
        return tuple(gaps)

    @staticmethod
    def _warnings(
        recommendations: list[TargetRecommendation],
        sequence_excluded: bool,
    ) -> tuple[AgentWarning, ...]:
        warnings = [
            AgentWarning(
                code="association_score_is_not_probability",
                message=(
                    "Open Targets association score는 질환 관련성 순위 휴리스틱이며 "
                    "druggability, 성공 확률 또는 임상적 확신도가 아닙니다."
                ),
            ),
            AgentWarning(
                code="model_rationale_is_interpretation",
                message=(
                    "후보 순위와 조절 방향은 조회된 필드에 대한 모델 해석이며 "
                    "독립적인 결합 또는 임상 근거가 아닙니다."
                ),
            ),
        ]
        if any(
            candidate.eligibility is TargetEligibility.EXPLORATORY for candidate in recommendations
        ):
            warnings.append(
                AgentWarning(
                    code="exploratory_target_in_shortlist",
                    message=(
                        "shortlist에 소분자 tractability 양성 근거가 확인되지 않은 "
                        "탐색 후보가 포함되어 있습니다."
                    ),
                )
            )
        if sequence_excluded:
            warnings.append(
                AgentWarning(
                    code="candidate_sequence_not_verified",
                    message=(
                        "일부 후보의 reviewed human protein sequence를 검증하지 못해 "
                        "DTA shortlist에서 제외했습니다."
                    ),
                )
            )
        if any(
            "therapeutic_direction_conflicting" in candidate.causal_support.reason_codes
            for candidate in recommendations
        ):
            warnings.append(
                AgentWarning(
                    code="therapeutic_direction_conflicting",
                    message=(
                        "일부 표적은 생물학적 인과 근거가 있지만 개별 근거가 서로 다른 "
                        "치료 조절 방향을 가리켜 방향을 unknown으로 유지했습니다."
                    ),
                )
            )
        if any(
            candidate.causal_support.clinical_validation_count
            and candidate.causal_support.biological_evidence_count == 0
            for candidate in recommendations
        ):
            warnings.append(
                AgentWarning(
                    code="clinical_validation_is_not_causal_support",
                    message=(
                        "임상 선행 근거는 치료 방향을 보강하지만 생물학적 인과성으로 "
                        "계산하지 않았습니다."
                    ),
                )
            )
        return tuple(warnings)

    def _failure(
        self,
        agent_input: AgentInput,
        started_at: datetime,
        started_clock: float,
        external_requests: int,
        *,
        code: str,
        message: str,
        retryable: bool,
        diagnostic_code: str | None = None,
        model: str | None = None,
        token_usage: ResponseUsage | None = None,
    ) -> AgentExecutionResult:
        output = AgentOutput[TargetHypothesisResult](
            analysis_id=agent_input.analysis_id,
            run_id=agent_input.run_id,
            agent_name=agent_input.agent_name,
            status=AgentOutputStatus.FAILED,
            error=ExecutionError(code=code, message=message, retryable=retryable),
            warnings=(
                AgentWarning(
                    code=diagnostic_code,
                    message="표적 모델 출력 검증의 세부 거부 유형입니다. 원문은 저장하지 않습니다.",
                ),
            )
            if diagnostic_code
            else (),
            execution_metadata=self._metadata(
                started_at,
                started_clock,
                external_requests,
                model=model,
                token_usage=token_usage,
            ),
        )
        return AgentExecutionResult(
            output=cast(AgentOutput[BaseModel], output),
            provides_dta_input=False,
        )

    def _metadata(
        self,
        started_at: datetime,
        started_clock: float,
        external_requests: int,
        *,
        source_version: str = "live-version-unavailable",
        model: str | None = None,
        token_usage: ResponseUsage | None = None,
    ) -> ExecutionMetadata:
        usage = None
        if token_usage is not None:
            usage = TokenUsage(
                input_tokens=token_usage.input_tokens,
                output_tokens=token_usage.output_tokens,
                total_tokens=token_usage.total_tokens,
            )
        components = [
            ComponentVersion(component="target_prompt", version=PROMPT_VERSION),
            ComponentVersion(component="target_policy", version="small-molecule-v1"),
            ComponentVersion(component="causal_policy", version="open-targets-v2"),
            ComponentVersion(component="open_targets", version=source_version),
            ComponentVersion(component="uniprotkb", version="rest-live"),
        ]
        if self._pharos_provider is not None:
            components.append(ComponentVersion(component="pharos", version="graphql-live"))
        if model is not None:
            components.append(ComponentVersion(component="language_model", version=model))
        return ExecutionMetadata(
            started_at=started_at,
            finished_at=datetime.now(UTC),
            duration_ms=max(0, round((perf_counter() - started_clock) * 1000)),
            implementation_version=IMPLEMENTATION_VERSION,
            components=tuple(components),
            usage=ExecutionUsage(
                token_usage=usage,
                external_requests=external_requests,
            ),
        )
