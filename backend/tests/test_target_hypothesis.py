import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from evidrug_api.analysis_input.models import AnalysisInputResponse, TargetMode
from evidrug_api.analysis_jobs.models import AnalysisStageName, AnalysisStageStatus, AnalysisStatus
from evidrug_api.analysis_jobs.repository import AnalysisRepository
from evidrug_api.analysis_jobs.service import _target_prioritization, read_analysis
from evidrug_api.analysis_jobs.tables import AnalysisRecord, AnalysisStageRecord
from evidrug_api.database import Base
from evidrug_api.execution_contracts.agent import AgentInput
from evidrug_api.execution_contracts.common import ExecutionLimits
from evidrug_api.openai_gateway.models import DaconQuota, GeneratedText, ResponseUsage
from evidrug_api.orchestration.executor import RoutedAgentExecutor
from evidrug_api.orchestration.service import AnalysisOrchestrator
from evidrug_api.orchestration.tables import AgentRunRecord, ExecutionTraceRecord
from evidrug_api.target_hypothesis.agent import TargetHypothesisAgent
from evidrug_api.target_hypothesis.causal_policy import CausalSupportPolicy
from evidrug_api.target_hypothesis.contracts import (
    AssessedTargetCandidate,
    CausalEvidenceAxis,
    CausalSupportStatus,
    DiseaseEvidenceScope,
    ModulationAction,
    PharosTargetEvidence,
    TargetCandidate,
    TargetCandidateBatch,
    TargetCausalEvidence,
    TargetDataTypeScore,
    TargetDevelopmentLevel,
    TargetEffectDirection,
    TargetEligibility,
    TargetHypothesisResult,
    TargetRanking,
    TargetRankingItem,
    TargetTractabilityAssessment,
    TraitEffectDirection,
    VerifiedProtein,
)
from evidrug_api.target_hypothesis.policy import SmallMoleculePrioritizationPolicy
from evidrug_api.target_hypothesis.providers import (
    OpenTargetsCandidateProvider,
    PharosProvider,
    PharosTargetProvider,
    PharosUnavailable,
    ProteinNotVerified,
    ProteinProvider,
    ProteinUnavailable,
    TargetCandidateProvider,
    TargetCandidatesNotFound,
    TargetCandidatesUnavailable,
    UniProtProteinProvider,
)
from evidrug_api.target_hypothesis.reasoner import (
    MAX_PROMPT_EVIDENCE_PER_CANDIDATE,
    DaconTargetReasoner,
    InvalidTargetSelection,
    ReasonedTargetRanking,
    TargetReasoner,
    TargetReasoningUnavailable,
)

BREAST_CARCINOMA_ID = "MONDO_0007254"
BREAST_CARCINOMA_NAME = "breast cancer"
CDK4_ENSEMBL_ID = "ENSG00000135446"
CDK4_UNIPROT_ACCESSION = "P11802"
BRCA2_ENSEMBL_ID = "ENSG00000139618"
BRCA2_UNIPROT_ACCESSION = "P51587"
PIK3CA_ENSEMBL_ID = "ENSG00000121879"
PALBOCICLIB_SMILES = "CC1=C(C(=O)N(C2=NC(=NC=C12)NC3=NC=C(C=C3)N4CCNCC4)C5CCCC5)C(=O)C"
PALBOCICLIB_CANONICAL_SMILES = "CC(=O)c1c(C)c2cnc(Nc3ccc(N4CCNCC4)cn3)nc2n(C2CCCC2)c1=O"
SOURCE_VERSION = "api-4.0.0/data-25.09"


def causal_evidence(
    *,
    evidence_id: str,
    datasource_id: str,
    axis: CausalEvidenceAxis,
    target_direction: TargetEffectDirection,
    trait_direction: TraitEffectDirection,
    scope: DiseaseEvidenceScope = DiseaseEvidenceScope.DIRECT,
) -> TargetCausalEvidence:
    return TargetCausalEvidence(
        evidence_id=evidence_id,
        datasource_id=datasource_id,
        datatype_id=(
            "clinical" if axis is CausalEvidenceAxis.CLINICAL_VALIDATION else "genetic_association"
        ),
        axis=axis,
        score=1.0,
        disease_id=(BREAST_CARCINOMA_ID if scope is DiseaseEvidenceScope.DIRECT else "EFO_1000016"),
        disease_name=(
            BREAST_CARCINOMA_NAME
            if scope is DiseaseEvidenceScope.DIRECT
            else "triple-negative breast cancer"
        ),
        disease_scope=scope,
        direction_on_target=target_direction,
        direction_on_trait=trait_direction,
    )


def analysis_input(*, target_mode: TargetMode = TargetMode.DISCOVER) -> AnalysisInputResponse:
    return AnalysisInputResponse(
        disease_id=BREAST_CARCINOMA_ID,
        disease_name=BREAST_CARCINOMA_NAME,
        target_mode=target_mode,
        target_name="CDK4" if target_mode is TargetMode.SPECIFIED else None,
        original_smiles=PALBOCICLIB_SMILES,
        canonical_smiles=PALBOCICLIB_CANONICAL_SMILES,
    )


def cdk4_candidate() -> TargetCandidate:
    return TargetCandidate(
        ensembl_id=CDK4_ENSEMBL_ID,
        approved_symbol="CDK4",
        approved_name="cyclin dependent kinase 4",
        biotype="protein_coding",
        function_descriptions=(
            "CDK4 promotes G1/S cell-cycle progression by phosphorylating RB1.",
        ),
        uniprot_accession=CDK4_UNIPROT_ACCESSION,
        association_score=0.73,
        data_type_scores=(TargetDataTypeScore(data_type="known_drug", score=0.81),),
        tractability_assessments=(
            TargetTractabilityAssessment(label="Approved Drug", value=True),
            TargetTractabilityAssessment(label="High-Quality Pocket", value=True),
            TargetTractabilityAssessment(label="Druggable Family", value=True),
        ),
        causal_evidence=(
            causal_evidence(
                evidence_id="clinical-cdk4",
                datasource_id="clinical_precedence",
                axis=CausalEvidenceAxis.CLINICAL_VALIDATION,
                target_direction=TargetEffectDirection.LOSS_OF_FUNCTION,
                trait_direction=TraitEffectDirection.PROTECTIVE,
            ),
        ),
    )


def brca2_candidate() -> TargetCandidate:
    return TargetCandidate(
        ensembl_id=BRCA2_ENSEMBL_ID,
        approved_symbol="BRCA2",
        approved_name="BRCA2 DNA repair associated",
        biotype="protein_coding",
        function_descriptions=("BRCA2 participates in homologous recombination repair.",),
        uniprot_accession=BRCA2_UNIPROT_ACCESSION,
        association_score=0.84,
        data_type_scores=(TargetDataTypeScore(data_type="genetic_association", score=0.94),),
        tractability_assessments=(
            TargetTractabilityAssessment(label="Approved Drug", value=False),
            TargetTractabilityAssessment(label="High-Quality Pocket", value=False),
            TargetTractabilityAssessment(label="Druggable Family", value=False),
        ),
        causal_evidence=(
            causal_evidence(
                evidence_id="burden-brca2",
                datasource_id="gene_burden",
                axis=CausalEvidenceAxis.STATISTICAL_GENETICS,
                target_direction=TargetEffectDirection.LOSS_OF_FUNCTION,
                trait_direction=TraitEffectDirection.RISK,
            ),
        ),
    )


def candidate_batch(*, specified: bool = False) -> TargetCandidateBatch:
    return TargetCandidateBatch(
        disease_id=BREAST_CARCINOMA_ID,
        disease_name=BREAST_CARCINOMA_NAME,
        candidates=(cdk4_candidate(),) if specified else (cdk4_candidate(), brca2_candidate()),
        source_version=SOURCE_VERSION,
        retrieved_at=datetime(2026, 9, 21, tzinfo=UTC),
        external_requests=3 if specified else 2,
    )


def verified_protein(accession: str) -> VerifiedProtein:
    if accession == CDK4_UNIPROT_ACCESSION:
        return VerifiedProtein(
            accession=accession,
            entry_name="CDK4_HUMAN",
            gene_symbol="CDK4",
            protein_name="Cyclin-dependent kinase 4",
            organism_taxon_id=9606,
            sequence="ACDEFGHIKLMNPQRSTVWY",
            retrieved_at=datetime(2026, 9, 21, 0, 0, 1, tzinfo=UTC),
        )
    if accession == BRCA2_UNIPROT_ACCESSION:
        return VerifiedProtein(
            accession=accession,
            entry_name="BRCA2_HUMAN",
            gene_symbol="BRCA2",
            protein_name="Breast cancer type 2 susceptibility protein",
            organism_taxon_id=9606,
            sequence="MNPQRSTVWYACDEFGHIKL",
            retrieved_at=datetime(2026, 9, 21, 0, 0, 2, tzinfo=UTC),
        )
    raise AssertionError("unexpected accession")


class FakeCandidateProvider:
    async def find_candidates(
        self,
        *,
        disease_id: str,
        target_name: str | None,
        limit: int,
    ) -> TargetCandidateBatch:
        assert disease_id == BREAST_CARCINOMA_ID
        assert limit == 5
        return candidate_batch(specified=target_name is not None)


class FakePharosProvider:
    async def get_target_evidence(self, accession: str) -> PharosTargetEvidence:
        return PharosTargetEvidence(
            uniprot_accession=accession,
            development_level=(
                TargetDevelopmentLevel.TCLIN
                if accession == CDK4_UNIPROT_ACCESSION
                else TargetDevelopmentLevel.TBIO
            ),
            target_family="Kinase" if accession == CDK4_UNIPROT_ACCESSION else None,
            novelty=0.01,
            ligand_count=12,
            publication_count=34,
            source_version="test-v1",
            retrieved_at=datetime(2026, 9, 21, tzinfo=UTC),
        )


class MissingPharosProvider:
    async def get_target_evidence(self, accession: str) -> None:
        return None


class UnavailablePharosProvider:
    async def get_target_evidence(self, accession: str) -> None:
        raise PharosUnavailable("unavailable")


class MissingCandidateProvider(FakeCandidateProvider):
    async def find_candidates(
        self,
        *,
        disease_id: str,
        target_name: str | None,
        limit: int,
    ) -> TargetCandidateBatch:
        raise TargetCandidatesNotFound("missing")


class UnavailableCandidateProvider(FakeCandidateProvider):
    async def find_candidates(
        self,
        *,
        disease_id: str,
        target_name: str | None,
        limit: int,
    ) -> TargetCandidateBatch:
        raise TargetCandidatesUnavailable("unavailable")


class BlockingCandidateProvider(FakeCandidateProvider):
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def find_candidates(
        self,
        *,
        disease_id: str,
        target_name: str | None,
        limit: int,
    ) -> TargetCandidateBatch:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled provider must not return")


class FakeProteinProvider:
    async def get_reviewed_human_protein(self, accession: str) -> VerifiedProtein:
        return verified_protein(accession)


class InvalidProteinProvider(FakeProteinProvider):
    async def get_reviewed_human_protein(self, accession: str) -> VerifiedProtein:
        raise ProteinNotVerified("invalid")


class InvalidAlternativeProteinProvider(FakeProteinProvider):
    async def get_reviewed_human_protein(self, accession: str) -> VerifiedProtein:
        if accession == BRCA2_UNIPROT_ACCESSION:
            raise ProteinNotVerified("invalid")
        return verified_protein(accession)


class UnavailableProteinProvider(FakeProteinProvider):
    async def get_reviewed_human_protein(self, accession: str) -> VerifiedProtein:
        raise ProteinUnavailable("unavailable")


class FakeReasoner:
    async def rank(
        self,
        case_input: AnalysisInputResponse,
        candidates: tuple[AssessedTargetCandidate, ...],
        *,
        shortlist_limit: int,
    ) -> ReasonedTargetRanking:
        requested = min(shortlist_limit, len(candidates))
        ranked = tuple(
            TargetRankingItem(
                ensembl_id=candidate.candidate.ensembl_id,
                modulation_action=candidate.causal_support.therapeutic_direction,
                rationale=("질환 관련성과 조회된 소분자 tractability 근거를 함께 고려했습니다."),
                causal_evidence_ids=tuple(
                    item.evidence_id for item in candidate.causal_support.evidence
                ),
                causal_rationale="제공된 개별 근거의 방향을 기준으로 해석했습니다.",
            )
            for candidate in candidates[:requested]
        )
        return ReasonedTargetRanking(
            ranking=TargetRanking(ranked_candidates=ranked),
            generated=GeneratedText(
                response_id="resp_1",
                model="gpt-5.6-luna",
                text="{}",
                usage=ResponseUsage(input_tokens=100, output_tokens=20, total_tokens=120),
                quota=DaconQuota(None, None, None, None),
            ),
        )


class InvalidReasoner(FakeReasoner):
    async def rank(
        self,
        case_input: AnalysisInputResponse,
        candidates: tuple[AssessedTargetCandidate, ...],
        *,
        shortlist_limit: int,
    ) -> ReasonedTargetRanking:
        raise InvalidTargetSelection("invalid")


class UnavailableReasoner(FakeReasoner):
    async def rank(
        self,
        case_input: AnalysisInputResponse,
        candidates: tuple[AssessedTargetCandidate, ...],
        *,
        shortlist_limit: int,
    ) -> ReasonedTargetRanking:
        raise TargetReasoningUnavailable("unavailable")


class ForbiddenReasoner(FakeReasoner):
    async def rank(
        self,
        case_input: AnalysisInputResponse,
        candidates: tuple[AssessedTargetCandidate, ...],
        *,
        shortlist_limit: int,
    ) -> ReasonedTargetRanking:
        raise AssertionError("reasoner must not run after Pharos failure")


class FakeDaconClient:
    def __init__(self, text: str) -> None:
        self.text = text

    async def generate_text(
        self,
        input_text: str,
        *,
        instructions: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> GeneratedText:
        assert max_output_tokens == 4096
        return GeneratedText(
            response_id="resp_1",
            model="gpt-5.6-luna",
            text=self.text,
            usage=None,
            quota=DaconQuota(None, None, None, None),
        )


def agent_input(*, target_mode: TargetMode = TargetMode.DISCOVER) -> AgentInput:
    return AgentInput(
        analysis_id=uuid4(),
        run_id=uuid4(),
        agent_name=AnalysisStageName.TARGET_HYPOTHESIS,
        attempt=1,
        case_input=analysis_input(target_mode=target_mode),
        execution_limits=ExecutionLimits(
            timeout_seconds=300,
            max_tool_calls=1,
            max_recall_depth=0,
        ),
    )


def target_agent(
    candidate_provider: TargetCandidateProvider | None = None,
    protein_provider: ProteinProvider | None = None,
    reasoner: TargetReasoner | None = None,
    pharos_provider: PharosProvider | None = None,
) -> TargetHypothesisAgent:
    return TargetHypothesisAgent(
        candidate_provider or FakeCandidateProvider(),
        protein_provider or FakeProteinProvider(),
        reasoner or FakeReasoner(),
        SmallMoleculePrioritizationPolicy(),
        candidate_limit=5,
        shortlist_limit=2,
        pharos_provider=pharos_provider,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target_mode", [TargetMode.DISCOVER, TargetMode.SPECIFIED])
async def test_target_agent_returns_verified_small_molecule_shortlist(
    target_mode: TargetMode,
) -> None:
    execution = await target_agent().execute(agent_input(target_mode=target_mode))

    assert execution.provides_dta_input is True
    assert execution.output.status.value == "completed"
    result = TargetHypothesisResult.model_validate(execution.output.result)
    assert result.primary.ensembl_id == CDK4_ENSEMBL_ID
    assert result.primary.uniprot_accession == CDK4_UNIPROT_ACCESSION
    assert result.primary.eligibility is TargetEligibility.ELIGIBLE
    assert result.primary.modulation_action is ModulationAction.INHIBIT
    assert result.primary.causal_evidence_ids == ("clinical-cdk4",)
    assert result.source_version == SOURCE_VERSION
    if target_mode is TargetMode.DISCOVER:
        assert [candidate.approved_symbol for candidate in result.alternatives] == ["BRCA2"]
        assert result.alternatives[0].eligibility is TargetEligibility.EXPLORATORY
        assert result.primary.causal_support.status is CausalSupportStatus.UNKNOWN
        assert result.primary.causal_support.clinical_validation_count == 1
        assert result.alternatives[0].causal_support.status is CausalSupportStatus.SUPPORTED
        assert result.alternatives[0].modulation_action is ModulationAction.ACTIVATE
        assert len(execution.output.gaps) == 2
        assert len(execution.output.evidence_claims) == 9
        assert execution.output.execution_metadata.usage.external_requests == 5
    else:
        assert result.alternatives == ()
        assert len(execution.output.gaps) == 1
        assert len(execution.output.evidence_claims) == 5
        assert execution.output.execution_metadata.usage.external_requests == 5


@pytest.mark.asyncio
async def test_target_agent_adds_pharos_maturity_without_overwriting_tractability() -> None:
    execution = await target_agent(pharos_provider=FakePharosProvider()).execute(agent_input())

    result = TargetHypothesisResult.model_validate(execution.output.result)
    assert result.primary.pharos_evidence is not None
    assert result.primary.pharos_evidence.development_level.value == "Tclin"
    assert result.primary.pharos_evidence.ligand_count == 12
    assert result.primary.eligibility is TargetEligibility.ELIGIBLE
    assert any(claim.claim_type == "target_maturity" for claim in execution.output.evidence_claims)
    assert execution.output.execution_metadata.usage.external_requests == 7


@pytest.mark.asyncio
async def test_target_agent_keeps_missing_pharos_as_evidence_gap() -> None:
    execution = await target_agent(pharos_provider=MissingPharosProvider()).execute(
        agent_input(target_mode=TargetMode.SPECIFIED)
    )

    result = TargetHypothesisResult.model_validate(execution.output.result)
    assert execution.output.status.value == "completed"
    assert result.primary.pharos_evidence is None
    assert any(gap.required_fields == ("target_maturity",) for gap in execution.output.gaps)


@pytest.mark.asyncio
async def test_target_agent_maps_pharos_failure_without_calling_reasoner() -> None:
    reasoner = ForbiddenReasoner()
    execution = await target_agent(
        reasoner=reasoner,
        pharos_provider=UnavailablePharosProvider(),
    ).execute(agent_input())

    assert execution.provides_dta_input is False
    assert execution.output.status.value == "failed"
    assert execution.output.error is not None
    assert execution.output.error.code == "pharos_unavailable"
    assert execution.output.error.retryable is True


@pytest.mark.asyncio
async def test_polling_projection_keeps_pre_causal_target_output_compatible() -> None:
    execution = await target_agent().execute(agent_input(target_mode=TargetMode.SPECIFIED))
    stored = execution.output.model_dump(mode="json")
    del stored["result"]["primary"]["causal_support"]
    del stored["result"]["primary"]["causal_evidence_ids"]
    del stored["result"]["primary"]["causal_rationale"]

    summary = _target_prioritization(json.dumps(stored))

    assert summary is not None
    assert summary.primary.causal_support.status == "unknown"
    assert summary.primary.causal_support.reason_codes == ("causal_evidence_missing",)
    assert summary.primary.causal_evidence_ids == ()


def test_policy_does_not_treat_missing_tractability_as_genetic_disease_exclusion() -> None:
    policy = SmallMoleculePrioritizationPolicy()

    eligible = policy.assess(cdk4_candidate())
    exploratory = policy.assess(brca2_candidate())

    assert eligible.eligibility is TargetEligibility.ELIGIBLE
    assert "approved_small_molecule" in eligible.reason_codes
    assert exploratory.eligibility is TargetEligibility.EXPLORATORY
    assert exploratory.reason_codes == ("small_molecule_tractability_evidence_missing",)


def test_causal_policy_separates_biological_support_from_clinical_validation() -> None:
    policy = CausalSupportPolicy()

    clinical_only = policy.assess(cdk4_candidate().causal_evidence)
    inherited_risk = policy.assess(brca2_candidate().causal_evidence)

    assert clinical_only.status is CausalSupportStatus.UNKNOWN
    assert clinical_only.clinical_validation_count == 1
    assert clinical_only.therapeutic_direction is ModulationAction.INHIBIT
    assert "causal_evidence_missing" in clinical_only.reason_codes
    assert inherited_risk.status is CausalSupportStatus.SUPPORTED
    assert inherited_risk.biological_evidence_count == 1
    assert inherited_risk.therapeutic_direction is ModulationAction.ACTIVATE


def test_causal_policy_keeps_support_when_only_therapeutic_direction_conflicts() -> None:
    evidence = (
        causal_evidence(
            evidence_id="somatic-pik3ca",
            datasource_id="intogen",
            axis=CausalEvidenceAxis.SOMATIC,
            target_direction=TargetEffectDirection.GAIN_OF_FUNCTION,
            trait_direction=TraitEffectDirection.RISK,
        ),
        causal_evidence(
            evidence_id="functional-pik3ca",
            datasource_id="crispr",
            axis=CausalEvidenceAxis.FUNCTIONAL,
            target_direction=TargetEffectDirection.LOSS_OF_FUNCTION,
            trait_direction=TraitEffectDirection.RISK,
            scope=DiseaseEvidenceScope.SUBTYPE,
        ),
    )

    support = CausalSupportPolicy().assess(evidence)

    assert support.status is CausalSupportStatus.SUPPORTED
    assert support.therapeutic_direction is ModulationAction.UNKNOWN
    assert "therapeutic_direction_conflicting" in support.reason_codes
    assert "causal_direction_conflicting" not in support.reason_codes
    assert "subtype_evidence_present" in support.reason_codes


def test_same_therapeutic_direction_is_not_conflict_and_opposition_is_retained() -> None:
    gof = causal_evidence(
        evidence_id="gof-risk",
        datasource_id="intogen",
        axis=CausalEvidenceAxis.SOMATIC,
        target_direction=TargetEffectDirection.GAIN_OF_FUNCTION,
        trait_direction=TraitEffectDirection.RISK,
    )
    lof = causal_evidence(
        evidence_id="lof-protect",
        datasource_id="clinical_precedence",
        axis=CausalEvidenceAxis.CLINICAL_VALIDATION,
        target_direction=TargetEffectDirection.LOSS_OF_FUNCTION,
        trait_direction=TraitEffectDirection.PROTECTIVE,
    )
    opposing = causal_evidence(
        evidence_id="lof-risk",
        datasource_id="eva",
        axis=CausalEvidenceAxis.CLINICAL_GENETICS,
        target_direction=TargetEffectDirection.LOSS_OF_FUNCTION,
        trait_direction=TraitEffectDirection.RISK,
        scope=DiseaseEvidenceScope.SUBTYPE,
    )
    assert (
        CausalSupportPolicy().assess((gof, lof)).therapeutic_direction is ModulationAction.INHIBIT
    )
    evidence = (gof, lof, opposing)
    support = CausalSupportPolicy().assess(evidence)
    assert support.therapeutic_direction is ModulationAction.UNKNOWN
    representatives = DaconTargetReasoner._representative_evidence(evidence)
    assert opposing in representatives
    assert {CausalSupportPolicy.therapeutic_action(e) for e in representatives} == {
        ModulationAction.INHIBIT,
        ModulationAction.ACTIVATE,
    }


def empty_evidence_payload(query: str) -> dict[str, object]:
    aliases = re.findall(r"\b(e\d+):\s*evidences", query)
    return {"data": {"disease": {alias: {"rows": []} for alias in aliases}}}


def evidence_payload(
    query: str,
    rows_by_source: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    selections = re.findall(
        r'\b(e\d+):\s*evidences\(\s*ensemblIds:\s*\["(ENSG\d+)"\].*?'
        r'datasourceIds:\s*\["([^"]+)"\]',
        query,
        re.DOTALL,
    )
    disease = {}
    for alias, target_id, datasource_id in selections:
        disease[alias] = {
            "rows": [
                {**row, "target": {"id": target_id}}
                for row in rows_by_source.get(datasource_id, [])
            ]
        }
    return {"data": {"disease": disease}}


@pytest.mark.asyncio
async def test_target_agent_isolates_unverified_alternative_sequence() -> None:
    execution = await target_agent(protein_provider=InvalidAlternativeProteinProvider()).execute(
        agent_input()
    )

    assert execution.output.status.value == "completed"
    result = TargetHypothesisResult.model_validate(execution.output.result)
    assert result.primary.approved_symbol == "CDK4"
    assert result.alternatives == ()
    assert any(
        candidate.approved_symbol == "BRCA2"
        and candidate.reason_code == "target_sequence_not_verified"
        for candidate in result.excluded_candidates
    )
    assert any(
        warning.code == "candidate_sequence_not_verified" for warning in execution.output.warnings
    )


@pytest.mark.asyncio
async def test_target_agent_preserves_expected_failure_without_dta_input() -> None:
    execution = await target_agent(candidate_provider=MissingCandidateProvider()).execute(
        agent_input()
    )

    assert execution.provides_dta_input is False
    assert execution.output.status.value == "failed"
    assert execution.output.error is not None
    assert execution.output.error.code == "target_not_found"
    assert execution.output.result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate_provider", "protein_provider", "reasoner", "expected_code", "retryable"),
    [
        (
            UnavailableCandidateProvider(),
            FakeProteinProvider(),
            FakeReasoner(),
            "target_evidence_unavailable",
            True,
        ),
        (
            FakeCandidateProvider(),
            FakeProteinProvider(),
            UnavailableReasoner(),
            "target_model_unavailable",
            True,
        ),
        (
            FakeCandidateProvider(),
            FakeProteinProvider(),
            InvalidReasoner(),
            "invalid_target_selection",
            False,
        ),
        (
            FakeCandidateProvider(),
            UnavailableProteinProvider(),
            FakeReasoner(),
            "target_sequence_unavailable",
            True,
        ),
        (
            FakeCandidateProvider(),
            InvalidProteinProvider(),
            FakeReasoner(),
            "target_sequence_not_verified",
            False,
        ),
    ],
)
async def test_target_agent_maps_provider_and_model_failures_to_stable_codes(
    candidate_provider: TargetCandidateProvider,
    protein_provider: ProteinProvider,
    reasoner: TargetReasoner,
    expected_code: str,
    retryable: bool,
) -> None:
    execution = await target_agent(
        candidate_provider=candidate_provider,
        protein_provider=protein_provider,
        reasoner=reasoner,
    ).execute(agent_input())

    assert execution.output.error is not None
    assert execution.output.error.code == expected_code
    assert execution.output.error.retryable is retryable
    assert execution.provides_dta_input is False


@pytest.mark.asyncio
async def test_target_agent_propagates_cancellation_to_orchestration() -> None:
    candidate_provider = BlockingCandidateProvider()
    executor = target_agent(candidate_provider=candidate_provider)
    task = asyncio.create_task(executor.execute(agent_input()))
    await asyncio.wait_for(candidate_provider.started.wait(), timeout=1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_target_agent_persists_safe_validation_diagnostic_and_usage() -> None:
    generated = GeneratedText(
        response_id="synthetic",
        model="gpt-5.6-luna",
        text="private model output",
        usage=ResponseUsage(input_tokens=30, output_tokens=12, total_tokens=42),
        quota=DaconQuota(None, None, None, None),
    )

    class InvalidWithDiagnostic(FakeReasoner):
        async def rank(
            self,
            case_input: AnalysisInputResponse,
            candidates: tuple[AssessedTargetCandidate, ...],
            *,
            shortlist_limit: int,
        ) -> ReasonedTargetRanking:
            raise InvalidTargetSelection(
                "private model output",
                diagnostic_code="target_direction_mismatch",
                generated=generated,
            )

    execution = await target_agent(reasoner=InvalidWithDiagnostic()).execute(agent_input())
    output = execution.output
    assert output.error is not None and output.error.code == "invalid_target_selection"
    assert [warning.code for warning in output.warnings] == ["target_direction_mismatch"]
    assert "private model output" not in output.model_dump_json()
    assert output.execution_metadata.usage.token_usage is not None
    assert output.execution_metadata.usage.token_usage.total_tokens == 42


@pytest.mark.asyncio
async def test_reasoner_rejects_identifier_outside_supplied_candidates() -> None:
    reasoner = DaconTargetReasoner(
        FakeDaconClient(
            json.dumps(
                {
                    "ranked_candidates": [
                        {
                            "ensembl_id": "ENSG99999999999",
                            "modulation_action": "unknown",
                            "rationale": "허용되지 않은 후보",
                        }
                    ]
                }
            )
        )  # type: ignore[arg-type]
    )
    assessed = SmallMoleculePrioritizationPolicy().assess_all(
        candidate_batch(specified=True).candidates
    )

    with pytest.raises(InvalidTargetSelection):
        await reasoner.rank(analysis_input(), assessed, shortlist_limit=1)


@pytest.mark.asyncio
async def test_reasoner_classifies_malformed_and_incomplete_responses() -> None:
    assessed = SmallMoleculePrioritizationPolicy().assess_all(
        candidate_batch(specified=True).candidates
    )
    reasoner = DaconTargetReasoner(FakeDaconClient("not json"))  # type: ignore[arg-type]
    with pytest.raises(InvalidTargetSelection) as malformed:
        await reasoner.rank(analysis_input(), assessed, shortlist_limit=1)
    assert malformed.value.diagnostic_code == "target_schema_invalid"

    class IncompleteClient(FakeDaconClient):
        async def generate_text(
            self,
            input_text: str,
            *,
            instructions: str | None = None,
            model: str | None = None,
            max_output_tokens: int | None = None,
        ) -> GeneratedText:
            result = await super().generate_text(
                input_text,
                instructions=instructions,
                model=model,
                max_output_tokens=max_output_tokens,
            )
            return replace(result, status="incomplete")

    reasoner = DaconTargetReasoner(IncompleteClient("{}"))  # type: ignore[arg-type]
    with pytest.raises(InvalidTargetSelection) as incomplete:
        await reasoner.rank(analysis_input(), assessed, shortlist_limit=1)
    assert incomplete.value.diagnostic_code == "target_response_incomplete"


@pytest.mark.asyncio
async def test_reasoner_accepts_only_supplied_causal_evidence_and_direction() -> None:
    assessed = SmallMoleculePrioritizationPolicy().assess_all(
        candidate_batch(specified=True).candidates
    )
    assessed = tuple(
        item.model_copy(
            update={"causal_support": CausalSupportPolicy().assess(item.candidate.causal_evidence)}
        )
        for item in assessed
    )
    reasoner = DaconTargetReasoner(
        FakeDaconClient(
            json.dumps(
                {
                    "ranked_candidates": [
                        {
                            "ensembl_id": CDK4_ENSEMBL_ID,
                            "modulation_action": "inhibit",
                            "rationale": "임상 근거와 tractability를 분리해 고려했습니다.",
                            "causal_evidence_ids": ["clinical-cdk4"],
                            "causal_rationale": (
                                "임상 선행 근거만 있어 생물학적 인과성은 unknown입니다."
                            ),
                        }
                    ]
                }
            )
        )  # type: ignore[arg-type]
    )

    result = await reasoner.rank(analysis_input(), assessed, shortlist_limit=1)

    assert result.ranking.ranked_candidates[0].causal_evidence_ids == ("clinical-cdk4",)


@pytest.mark.asyncio
async def test_reasoner_rejects_excessive_decimal_precision_in_reader_prose() -> None:
    assessed = SmallMoleculePrioritizationPolicy().assess_all(
        candidate_batch(specified=True).candidates
    )
    assessed = tuple(
        item.model_copy(
            update={"causal_support": CausalSupportPolicy().assess(item.candidate.causal_evidence)}
        )
        for item in assessed
    )
    reasoner = DaconTargetReasoner(
        FakeDaconClient(
            json.dumps(
                {
                    "ranked_candidates": [
                        {
                            "ensembl_id": CDK4_ENSEMBL_ID,
                            "modulation_action": "inhibit",
                            "rationale": "연관 점수는 0.6306613832534401입니다.",
                            "causal_evidence_ids": ["clinical-cdk4"],
                            "causal_rationale": "임상 검증과 생물학적 인과성을 구분합니다.",
                        }
                    ]
                }
            )
        )  # type: ignore[arg-type]
    )

    with pytest.raises(InvalidTargetSelection) as invalid:
        await reasoner.rank(analysis_input(), assessed, shortlist_limit=1)

    assert invalid.value.diagnostic_code == "target_number_precision_invalid"


@pytest.mark.asyncio
async def test_reasoner_keeps_raw_evidence_ids_out_of_prose() -> None:
    opaque_id = "216fdca91a1f3ea61fa228347523692c19838ab6"
    evidence = (
        causal_evidence(
            evidence_id=opaque_id,
            datasource_id="gene_burden",
            axis=CausalEvidenceAxis.STATISTICAL_GENETICS,
            target_direction=TargetEffectDirection.LOSS_OF_FUNCTION,
            trait_direction=TraitEffectDirection.PROTECTIVE,
        ),
    )
    candidate = cdk4_candidate().model_copy(update={"causal_evidence": evidence})
    assessed = (
        SmallMoleculePrioritizationPolicy()
        .assess(candidate)
        .model_copy(update={"causal_support": CausalSupportPolicy().assess(evidence)})
    )
    reasoner = DaconTargetReasoner(
        FakeDaconClient(
            json.dumps(
                {
                    "ranked_candidates": [
                        {
                            "ensembl_id": CDK4_ENSEMBL_ID,
                            "modulation_action": "inhibit",
                            "rationale": f"{opaque_id}를 우선 근거로 고려했습니다.",
                            "causal_evidence_ids": [opaque_id],
                            "causal_rationale": (
                                f"직접 근거인 {opaque_id}는 loss_of_function/protective로 "
                                "억제를 시사합니다."
                            ),
                        }
                    ]
                }
            )
        )  # type: ignore[arg-type]
    )

    result = await reasoner.rank(analysis_input(), (assessed,), shortlist_limit=1)
    ranked = result.ranking.ranked_candidates[0]

    assert ranked.causal_evidence_ids == (opaque_id,)
    assert opaque_id not in ranked.rationale
    assert opaque_id not in ranked.causal_rationale
    assert "직접 질환 통계 유전학 근거 1" in ranked.rationale
    assert "직접 질환 통계 유전학 근거 1" in ranked.causal_rationale


def test_reasoner_labels_subtype_clinical_validation_without_raw_id() -> None:
    evidence = (
        causal_evidence(
            evidence_id="192a28aa625b1caa2294b8bbbfd08cb5d10ad36e",
            datasource_id="clinical_precedence",
            axis=CausalEvidenceAxis.CLINICAL_VALIDATION,
            target_direction=TargetEffectDirection.UNKNOWN,
            trait_direction=TraitEffectDirection.UNKNOWN,
            scope=DiseaseEvidenceScope.SUBTYPE,
        ),
    )

    labels = DaconTargetReasoner._evidence_labels(evidence)

    assert labels[evidence[0].evidence_id] == "하위 유형 임상 검증 근거 1"


def test_reasoner_uses_bounded_representative_evidence_with_full_counts() -> None:
    evidence = tuple(
        causal_evidence(
            evidence_id=f"evidence-{axis.value}-{index}",
            datasource_id=f"source-{axis.value}-{index}",
            axis=axis,
            target_direction=(
                TargetEffectDirection.GAIN_OF_FUNCTION
                if index % 2 == 0
                else TargetEffectDirection.LOSS_OF_FUNCTION
            ),
            trait_direction=(
                TraitEffectDirection.RISK if index < 2 else TraitEffectDirection.PROTECTIVE
            ),
            scope=(DiseaseEvidenceScope.DIRECT if index % 2 == 0 else DiseaseEvidenceScope.SUBTYPE),
        )
        for axis in CausalEvidenceAxis
        for index in range(4)
    )
    candidate = cdk4_candidate().model_copy(update={"causal_evidence": evidence})
    assessed = (
        SmallMoleculePrioritizationPolicy()
        .assess(candidate)
        .model_copy(update={"causal_support": CausalSupportPolicy().assess(evidence)})
    )

    representative = DaconTargetReasoner._representative_evidence(evidence)
    prompt_candidate = DaconTargetReasoner._prompt_candidate(assessed, representative)
    prompt_support = prompt_candidate["causal_support"]

    assert isinstance(prompt_support, dict)
    assert len(representative) == MAX_PROMPT_EVIDENCE_PER_CANDIDATE
    assert {item.axis for item in representative} == set(CausalEvidenceAxis)
    assert prompt_support["evidence_total_count"] == len(evidence)
    assert len(prompt_support["representative_evidence"]) == len(representative)
    assert all(item["evidence_label"] for item in prompt_support["representative_evidence"])
    assert "evidence" not in prompt_support
    requirements = cast(dict[str, object], prompt_candidate["citation_requirements"])
    assert requirements["minimum_ids"] == 1
    action_groups = cast(dict[str, list[str]], requirements["required_action_groups"])
    assert set(action_groups) == {"inhibit", "activate"}
    assert all(action_groups[action] for action in action_groups)
    representative_ids = {item.evidence_id for item in representative}
    assert all(set(ids) <= representative_ids for ids in action_groups.values())


def test_reasoner_prompt_requires_citation_only_when_evidence_exists() -> None:
    candidate = cdk4_candidate().model_copy(update={"causal_evidence": ()})
    assessed = (
        SmallMoleculePrioritizationPolicy()
        .assess(candidate)
        .model_copy(update={"causal_support": CausalSupportPolicy().assess(())})
    )
    prompt_candidate = DaconTargetReasoner._prompt_candidate(assessed, ())
    assert prompt_candidate["citation_requirements"] == {
        "minimum_ids": 0,
        "required_action_groups": {},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evidence_ids", "modulation_action"),
    [(["invented-evidence"], "inhibit"), (["clinical-cdk4"], "activate")],
)
async def test_reasoner_rejects_invented_causal_evidence_or_direction(
    evidence_ids: list[str],
    modulation_action: str,
) -> None:
    assessed = SmallMoleculePrioritizationPolicy().assess_all(
        candidate_batch(specified=True).candidates
    )
    assessed = tuple(
        item.model_copy(
            update={"causal_support": CausalSupportPolicy().assess(item.candidate.causal_evidence)}
        )
        for item in assessed
    )
    reasoner = DaconTargetReasoner(
        FakeDaconClient(
            json.dumps(
                {
                    "ranked_candidates": [
                        {
                            "ensembl_id": CDK4_ENSEMBL_ID,
                            "modulation_action": modulation_action,
                            "rationale": "검증 대상",
                            "causal_evidence_ids": evidence_ids,
                            "causal_rationale": "검증 대상",
                        }
                    ]
                }
            )
        )  # type: ignore[arg-type]
    )

    with pytest.raises(InvalidTargetSelection):
        await reasoner.rank(analysis_input(), assessed, shortlist_limit=1)


@pytest.mark.asyncio
async def test_open_targets_provider_preserves_tractability_version_and_exclusions() -> None:
    response_payload = {
        "data": {
            "meta": {
                "apiVersion": {"x": 4, "y": 0, "z": 0, "suffix": None},
                "dataVersion": {"year": 25, "month": 9, "iteration": None},
            },
            "disease": {
                "id": BREAST_CARCINOMA_ID,
                "name": BREAST_CARCINOMA_NAME,
                "associatedTargets": {
                    "rows": [
                        {
                            "score": 0.73,
                            "datatypeScores": [{"id": "known_drug", "score": 0.81}],
                            "target": {
                                "id": CDK4_ENSEMBL_ID,
                                "approvedSymbol": "CDK4",
                                "approvedName": "cyclin dependent kinase 4",
                                "biotype": "protein_coding",
                                "functionDescriptions": ["Cell-cycle kinase."],
                                "proteinIds": [
                                    {
                                        "id": CDK4_UNIPROT_ACCESSION,
                                        "source": "uniprot_swissprot",
                                    }
                                ],
                                "tractability": [
                                    {"label": "Approved Drug", "modality": "SM", "value": True},
                                    {
                                        "label": "UniProt loc high conf",
                                        "modality": "AB",
                                        "value": False,
                                    },
                                ],
                            },
                        },
                        {
                            "score": 0.05,
                            "datatypeScores": [],
                            "target": {
                                "id": "ENSG00000273796",
                                "approvedSymbol": "BNAT1",
                                "approvedName": "neighboring non-coding transcript",
                                "biotype": "lncRNA",
                                "functionDescriptions": [],
                                "proteinIds": [],
                                "tractability": [],
                            },
                        },
                    ]
                },
            },
        }
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "TargetCausalEvidence" in body["query"]:
            return httpx.Response(
                200,
                json=evidence_payload(
                    body["query"],
                    {
                        "gene_burden": [
                            {
                                "id": "burden-cdk4",
                                "datasourceId": "gene_burden",
                                "datatypeId": "genetic_association",
                                "score": 0.9,
                                "directionOnTrait": "risk",
                                "directionOnTarget": "GoF",
                                "targetRole": None,
                                "confidence": None,
                                "significantDriverMethods": [],
                                "disease": {
                                    "id": BREAST_CARCINOMA_ID,
                                    "name": BREAST_CARCINOMA_NAME,
                                },
                            }
                        ],
                        "clinical_precedence": [
                            {
                                "id": "clinical-cdk4-subtype",
                                "datasourceId": "clinical_precedence",
                                "datatypeId": "clinical",
                                "score": 1.0,
                                "directionOnTrait": "protect",
                                "directionOnTarget": "LoF",
                                "targetRole": None,
                                "confidence": None,
                                "significantDriverMethods": [],
                                "disease": {
                                    "id": "EFO_1000016",
                                    "name": "triple-negative breast cancer",
                                },
                            }
                        ],
                    },
                ),
            )
        assert body["variables"]["filter"] is None
        return httpx.Response(200, json=response_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenTargetsCandidateProvider(client, "https://example.test/graphql")
        batch = await provider.find_candidates(
            disease_id=BREAST_CARCINOMA_ID,
            target_name=None,
            limit=5,
        )

    assert batch.source_version == SOURCE_VERSION
    assert batch.external_requests == 2
    assert [candidate.approved_symbol for candidate in batch.candidates] == ["CDK4"]
    assert batch.candidates[0].tractability_assessments == (
        TargetTractabilityAssessment(label="Approved Drug", value=True),
    )
    assert [item.datasource_id for item in batch.candidates[0].causal_evidence] == [
        "gene_burden",
        "clinical_precedence",
    ]
    assert batch.candidates[0].causal_evidence[0].disease_scope is DiseaseEvidenceScope.DIRECT
    assert batch.candidates[0].causal_evidence[1].disease_scope is DiseaseEvidenceScope.SUBTYPE
    assert batch.excluded_candidates[0].reason_code == "reviewed_protein_unavailable"


@pytest.mark.asyncio
async def test_open_targets_provider_keeps_exact_specified_target() -> None:
    response_payload = {
        "data": {
            "meta": {
                "apiVersion": {"x": 4, "y": 0, "z": 0, "suffix": None},
                "dataVersion": {"year": 25, "month": 9, "iteration": None},
            },
            "disease": {
                "id": BREAST_CARCINOMA_ID,
                "name": BREAST_CARCINOMA_NAME,
                "associatedTargets": {
                    "rows": [
                        {
                            "score": 0.73,
                            "datatypeScores": [],
                            "target": {
                                "id": CDK4_ENSEMBL_ID,
                                "approvedSymbol": "CDK4",
                                "approvedName": "cyclin dependent kinase 4",
                                "biotype": "protein_coding",
                                "functionDescriptions": ["Cell-cycle kinase."],
                                "proteinIds": [
                                    {
                                        "id": CDK4_UNIPROT_ACCESSION,
                                        "source": "uniprot_swissprot",
                                    }
                                ],
                                "tractability": [],
                            },
                        }
                    ]
                },
            },
        }
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "SearchTarget" in body["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "search": {
                            "hits": [
                                {
                                    "id": CDK4_ENSEMBL_ID,
                                    "name": "CDK4",
                                    "description": "cyclin dependent kinase 4",
                                    "score": 100.0,
                                }
                            ]
                        }
                    }
                },
            )
        if "TargetCausalEvidence" in body["query"]:
            return httpx.Response(200, json=empty_evidence_payload(body["query"]))
        assert body["variables"]["filter"] == CDK4_ENSEMBL_ID
        return httpx.Response(200, json=response_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenTargetsCandidateProvider(client, "https://example.test/graphql")
        batch = await provider.find_candidates(
            disease_id=BREAST_CARCINOMA_ID,
            target_name="CDK4",
            limit=5,
        )

    assert [candidate.approved_symbol for candidate in batch.candidates] == ["CDK4"]
    assert batch.external_requests == 3


@pytest.mark.asyncio
async def test_open_targets_provider_maps_timeout_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenTargetsCandidateProvider(client, "https://example.test/graphql")
        with pytest.raises(TargetCandidatesUnavailable):
            await provider.find_candidates(
                disease_id=BREAST_CARCINOMA_ID,
                target_name=None,
                limit=5,
            )


@pytest.mark.asyncio
async def test_pharos_provider_parses_target_maturity() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["variables"] == {"uniprot": CDK4_UNIPROT_ACCESSION}
        return httpx.Response(
            200,
            json={
                "data": {
                    "target": {
                        "uniprot": CDK4_UNIPROT_ACCESSION,
                        "tdl": "Tclin",
                        "fam": "Kinase",
                        "novelty": 0.00036742,
                        "ligandCounts": [
                            {"name": "ligand", "value": 526},
                            {"name": "drug", "value": 4},
                        ],
                        "publicationCount": 508,
                    }
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PharosTargetProvider(
            client, "https://pharos.test/graphql"
        ).get_target_evidence(CDK4_UNIPROT_ACCESSION)

    assert result is not None
    assert result.development_level.value == "Tclin"
    assert result.target_family == "Kinase"
    assert result.ligand_count == 526
    assert result.publication_count == 508


@pytest.mark.asyncio
async def test_pharos_provider_returns_none_when_target_is_not_found() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"target": None}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PharosTargetProvider(
            client, "https://pharos.test/graphql"
        ).get_target_evidence(CDK4_UNIPROT_ACCESSION)

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_payload",
    [
        {"errors": [{"message": "upstream unavailable"}]},
        {
            "data": {
                "target": {
                    "uniprot": BRCA2_UNIPROT_ACCESSION,
                    "tdl": "Tclin",
                    "ligandCounts": [],
                    "publicationCount": 0,
                }
            }
        },
        {
            "data": {
                "target": {
                    "uniprot": CDK4_UNIPROT_ACCESSION,
                    "tdl": "unsupported",
                    "ligandCounts": [],
                    "publicationCount": 0,
                }
            }
        },
    ],
)
async def test_pharos_provider_rejects_invalid_responses(
    response_payload: dict[str, object],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PharosUnavailable):
            await PharosTargetProvider(client, "https://pharos.test/graphql").get_target_evidence(
                CDK4_UNIPROT_ACCESSION
            )


@pytest.mark.asyncio
async def test_pharos_provider_maps_timeout_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PharosUnavailable):
            await PharosTargetProvider(client, "https://pharos.test/graphql").get_target_evidence(
                CDK4_UNIPROT_ACCESSION
            )


@pytest.mark.asyncio
async def test_uniprot_provider_rejects_unreviewed_entry() -> None:
    payload = {
        "entryType": "UniProtKB unreviewed (TrEMBL)",
        "primaryAccession": "A0A000",
        "uniProtkbId": "EXAMPLE_HUMAN",
        "organism": {"taxonId": 9606},
        "proteinDescription": {"recommendedName": {"fullName": {"value": "Example"}}},
        "genes": [{"geneName": {"value": "EXAMPLE"}}],
        "sequence": {"value": "ACDEFGHIK"},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = UniProtProteinProvider(client, "https://example.test")
        with pytest.raises(ProteinNotVerified):
            await provider.get_reviewed_human_protein("A0A000")


@pytest_asyncio.fixture
async def orchestration_database(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target-agent.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def create_analysis(factory: async_sessionmaker[AsyncSession]) -> UUID:
    case = analysis_input(target_mode=TargetMode.SPECIFIED)
    async with factory() as session:
        analysis = AnalysisRecord(
            session_fingerprint="a" * 64,
            idempotency_key=str(uuid4()),
            disease_id=case.disease_id,
            disease_name=case.disease_name,
            target_mode=case.target_mode,
            target_name=case.target_name,
            original_smiles=case.original_smiles,
            canonical_smiles=case.canonical_smiles,
            stages=[
                AnalysisStageRecord(name=name, position=position)
                for position, name in enumerate(AnalysisStageName, start=1)
            ],
        )
        session.add(analysis)
        await session.commit()
        return analysis.id


@pytest.mark.asyncio
async def test_orchestration_persists_ranked_target_output_before_unconfigured_stages_fail(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    analysis_id = await create_analysis(orchestration_database)
    executor = RoutedAgentExecutor({AnalysisStageName.TARGET_HYPOTHESIS: target_agent()})

    status = await AnalysisOrchestrator(orchestration_database, executor).run(analysis_id)

    assert status is AnalysisStatus.FAILED
    async with orchestration_database() as session:
        analysis = await session.get(AnalysisRecord, analysis_id)
        assert analysis is not None
        assert analysis.error_code == "decision_failed"
        stages = {
            stage.name: stage.status
            for stage in await session.scalars(
                select(AnalysisStageRecord).where(AnalysisStageRecord.analysis_id == analysis_id)
            )
        }
        assert stages[AnalysisStageName.TARGET_HYPOTHESIS] is AnalysisStageStatus.COMPLETED
        assert stages[AnalysisStageName.DTA] is AnalysisStageStatus.FAILED

        target_run = await session.scalar(
            select(AgentRunRecord).where(
                AgentRunRecord.analysis_id == analysis_id,
                AgentRunRecord.agent_name == AnalysisStageName.TARGET_HYPOTHESIS,
            )
        )
        assert target_run is not None
        assert target_run.output_json is not None
        stored_output = json.loads(target_run.output_json)
        assert stored_output["result"]["primary"]["uniprot_accession"] == (CDK4_UNIPROT_ACCESSION)
        assert stored_output["result"]["modality"] == "small_molecule"

        from evidrug_api.analysis_jobs.service import fingerprint_session

        analysis.session_fingerprint = fingerprint_session("owner-session")
        await session.commit()
        response = await read_analysis(analysis_id, AnalysisRepository(session), "owner-session")
        assert response.target_prioritization is not None
        assert response.target_prioritization.primary.approved_symbol == "CDK4"
        assert response.target_prioritization.primary.eligibility == "eligible"
        assert response.target_prioritization.excluded_candidates == ()
        assert "target_sequence" not in response.target_prioritization.primary.model_dump()

        target_events = list(
            await session.scalars(
                select(ExecutionTraceRecord.event_type).where(
                    ExecutionTraceRecord.analysis_id == analysis_id,
                    ExecutionTraceRecord.stage_name == AnalysisStageName.TARGET_HYPOTHESIS,
                )
            )
        )
        assert target_events == ["stage_started", "stage_completed"]
