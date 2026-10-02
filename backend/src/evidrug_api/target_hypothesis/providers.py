"""Open Targets·Pharos 근거와 UniProt protein sequence 조회 경계."""

from datetime import UTC, datetime
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evidrug_api.target_hypothesis.contracts import (
    CausalEvidenceAxis,
    DiseaseEvidenceScope,
    ExcludedTargetCandidate,
    PharosTargetEvidence,
    TargetCandidate,
    TargetCandidateBatch,
    TargetCausalEvidence,
    TargetDataTypeScore,
    TargetDevelopmentLevel,
    TargetEffectDirection,
    TargetTractabilityAssessment,
    TraitEffectDirection,
    VerifiedProtein,
)

PHAROS_TARGET_QUERY = """
query PharosTarget($uniprot: String!) {
  target(q: {uniprot: $uniprot}) {
    uniprot
    tdl
    fam
    novelty
    ligandCounts { name value }
    publicationCount
  }
}
"""

DISEASE_TARGETS_QUERY = """
query DiseaseTargets($diseaseId: String!, $filter: String, $size: Int!) {
  meta {
    apiVersion { x y z suffix }
    dataVersion { year month iteration }
  }
  disease(efoId: $diseaseId) {
    id
    name
    associatedTargets(
      BFilter: $filter
      enableIndirect: false
      page: {index: 0, size: $size}
    ) {
      rows {
        score
        datatypeScores { id score }
        target {
          id
          approvedSymbol
          approvedName
          biotype
          functionDescriptions
          proteinIds { id source }
          tractability { label modality value }
        }
      }
    }
  }
}
"""

TARGET_SEARCH_QUERY = """
query SearchTarget($query: String!, $size: Int!) {
  search(
    queryString: $query
    entityNames: ["target"]
    page: {index: 0, size: $size}
  ) {
    hits { id name description score }
  }
}
"""

EVIDENCE_DATASOURCES: dict[CausalEvidenceAxis, tuple[str, ...]] = {
    CausalEvidenceAxis.STATISTICAL_GENETICS: (
        "gwas_credible_sets",
        "gene_burden",
    ),
    CausalEvidenceAxis.CLINICAL_GENETICS: (
        "eva",
        "gene2phenotype",
        "orphanet",
    ),
    CausalEvidenceAxis.SOMATIC: (
        "eva_somatic",
        "cancer_gene_census",
        "intogen",
    ),
    CausalEvidenceAxis.FUNCTIONAL: ("crispr",),
    CausalEvidenceAxis.CLINICAL_VALIDATION: ("clinical_precedence",),
}
EVIDENCE_PER_DATASOURCE = 5


class TargetCandidatesUnavailable(RuntimeError):
    """Open Targets가 검증 가능한 후보 응답을 제공하지 못했다."""


class TargetCandidatesNotFound(RuntimeError):
    """질환과 입력 조건에 맞는 reviewed protein 표적이 없다."""


class ProteinUnavailable(RuntimeError):
    """UniProt가 요청한 protein 응답을 제공하지 못했다."""


class ProteinNotVerified(RuntimeError):
    """UniProt 응답이 reviewed human protein 계약을 충족하지 못했다."""


class PharosUnavailable(RuntimeError):
    """Pharos가 검증 가능한 표적 성숙도 응답을 제공하지 못했다."""


class TargetCandidateProvider(Protocol):
    async def find_candidates(
        self,
        *,
        disease_id: str,
        target_name: str | None,
        limit: int,
    ) -> TargetCandidateBatch: ...


class ProteinProvider(Protocol):
    async def get_reviewed_human_protein(self, accession: str) -> VerifiedProtein: ...


class PharosProvider(Protocol):
    async def get_target_evidence(self, accession: str) -> PharosTargetEvidence | None: ...


class _GraphQLError(BaseModel):
    message: str


class _PharosCount(BaseModel):
    name: str
    value: int


class _PharosTarget(BaseModel):
    uniprot: str
    tdl: str
    fam: str | None = None
    novelty: float | None = None
    ligandCounts: list[_PharosCount] = Field(default_factory=list)
    publicationCount: int = 0


class _PharosData(BaseModel):
    target: _PharosTarget | None = None


class _PharosResponse(BaseModel):
    data: _PharosData | None = None
    errors: list[_GraphQLError] = Field(default_factory=list)


class PharosTargetProvider:
    """UniProt accession을 Pharos의 표적 성숙도·지식량 관측으로 변환한다."""

    def __init__(self, client: httpx.AsyncClient, graphql_url: str) -> None:
        self._client = client
        self._graphql_url = graphql_url

    async def get_target_evidence(self, accession: str) -> PharosTargetEvidence | None:
        try:
            response = await self._client.post(
                self._graphql_url,
                json={"query": PHAROS_TARGET_QUERY, "variables": {"uniprot": accession}},
            )
            response.raise_for_status()
            payload = _PharosResponse.model_validate(response.json())
            if payload.errors or payload.data is None:
                raise PharosUnavailable("Pharos target lookup is unavailable")
            target = payload.data.target
            if target is None:
                return None
            if target.uniprot != accession:
                raise PharosUnavailable("Pharos returned a different target")
            ligand_count = next(
                (item.value for item in target.ligandCounts if item.name == "ligand"), 0
            )
            return PharosTargetEvidence(
                uniprot_accession=target.uniprot,
                development_level=TargetDevelopmentLevel(target.tdl),
                target_family=target.fam,
                novelty=target.novelty,
                ligand_count=ligand_count,
                publication_count=target.publicationCount,
                source_version="graphql-live",
                retrieved_at=datetime.now(UTC),
            )
        except PharosUnavailable:
            raise
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            raise PharosUnavailable("Pharos target lookup is unavailable") from error


class _ProteinId(BaseModel):
    id: str
    source: str


class _Score(BaseModel):
    id: str
    score: float


class _Tractability(BaseModel):
    label: str
    modality: str
    value: bool


class _Target(BaseModel):
    id: str
    approvedSymbol: str
    approvedName: str
    biotype: str
    functionDescriptions: list[str] = Field(default_factory=list)
    proteinIds: list[_ProteinId] = Field(default_factory=list)
    tractability: list[_Tractability] = Field(default_factory=list)


class _AssociatedTarget(BaseModel):
    score: float
    datatypeScores: list[_Score] = Field(default_factory=list)
    target: _Target


class _AssociatedTargets(BaseModel):
    rows: list[_AssociatedTarget] = Field(default_factory=list)


class _Disease(BaseModel):
    id: str
    name: str
    associatedTargets: _AssociatedTargets


class _ApiVersion(BaseModel):
    x: int
    y: int
    z: int
    suffix: str | None = None


class _DataVersion(BaseModel):
    year: int
    month: int
    iteration: int | None = None


class _Meta(BaseModel):
    apiVersion: _ApiVersion
    dataVersion: _DataVersion


class _DiseaseTargetData(BaseModel):
    meta: _Meta
    disease: _Disease | None = None


class _DiseaseTargetResponse(BaseModel):
    data: _DiseaseTargetData | None = None
    errors: list[_GraphQLError] = Field(default_factory=list)


class _SearchHit(BaseModel):
    id: str
    name: str
    description: str | None = None
    score: float


class _SearchResults(BaseModel):
    hits: list[_SearchHit] = Field(default_factory=list)


class _SearchData(BaseModel):
    search: _SearchResults


class _TargetSearchResponse(BaseModel):
    data: _SearchData | None = None
    errors: list[_GraphQLError] = Field(default_factory=list)


class _EvidenceEntity(BaseModel):
    id: str
    name: str


class _EvidenceTarget(BaseModel):
    id: str


class _EvidenceRecord(BaseModel):
    id: str
    datasourceId: str
    datatypeId: str
    score: float
    directionOnTrait: str | None = None
    directionOnTarget: str | None = None
    targetRole: str | None = None
    confidence: str | None = None
    significantDriverMethods: list[str] = Field(default_factory=list)
    disease: _EvidenceEntity
    target: _EvidenceTarget


class _EvidenceRows(BaseModel):
    rows: list[_EvidenceRecord] = Field(default_factory=list)


class _EvidenceData(BaseModel):
    disease: dict[str, _EvidenceRows] | None = None


class _EvidenceResponse(BaseModel):
    data: _EvidenceData | None = None
    errors: list[_GraphQLError] = Field(default_factory=list)


class OpenTargetsCandidateProvider:
    """질환별 direct association 순위에서 reviewed protein 후보만 반환한다."""

    def __init__(self, client: httpx.AsyncClient, graphql_url: str) -> None:
        self._client = client
        self._graphql_url = graphql_url

    async def find_candidates(
        self,
        *,
        disease_id: str,
        target_name: str | None,
        limit: int,
    ) -> TargetCandidateBatch:
        resolved_target_id = None
        request_count = 1
        if target_name:
            resolved_target_id = await self._resolve_target(target_name)
            request_count += 1

        try:
            response = await self._client.post(
                self._graphql_url,
                json={
                    "query": DISEASE_TARGETS_QUERY,
                    "variables": {
                        "diseaseId": disease_id,
                        "filter": resolved_target_id,
                        "size": limit,
                    },
                },
            )
            response.raise_for_status()
            payload = _DiseaseTargetResponse.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            raise TargetCandidatesUnavailable(
                "Open Targets target lookup is unavailable"
            ) from error

        if payload.errors or payload.data is None:
            raise TargetCandidatesUnavailable("Open Targets target lookup is unavailable")
        if payload.data.disease is None:
            raise TargetCandidatesNotFound("disease was not found in Open Targets")

        disease = payload.data.disease
        parsed = tuple(self._candidate(row) for row in disease.associatedTargets.rows)
        candidates = tuple(item for item in parsed if isinstance(item, TargetCandidate))
        excluded_candidates = tuple(
            item for item in parsed if isinstance(item, ExcludedTargetCandidate)
        )
        if target_name:
            exact = tuple(
                candidate for candidate in candidates if candidate.ensembl_id == resolved_target_id
            )
            if not exact:
                raise TargetCandidatesNotFound(
                    "specified target did not resolve to a reviewed protein"
                )
            candidates = exact
            excluded_candidates = tuple(
                candidate
                for candidate in excluded_candidates
                if candidate.ensembl_id == resolved_target_id
            )
        if not candidates:
            raise TargetCandidatesNotFound("no reviewed protein target candidate was found")

        evidence_by_target = await self._find_evidence(
            disease_id=disease.id,
            candidates=candidates,
        )
        request_count += 1
        candidates = tuple(
            candidate.model_copy(
                update={"causal_evidence": evidence_by_target.get(candidate.ensembl_id, ())}
            )
            for candidate in candidates
        )

        return TargetCandidateBatch(
            disease_id=disease.id,
            disease_name=disease.name,
            candidates=candidates,
            excluded_candidates=excluded_candidates,
            source_version=self._source_version(payload.data.meta),
            retrieved_at=datetime.now(UTC),
            external_requests=request_count,
        )

    async def _find_evidence(
        self,
        *,
        disease_id: str,
        candidates: tuple[TargetCandidate, ...],
    ) -> dict[str, tuple[TargetCausalEvidence, ...]]:
        query, aliases = self._evidence_query(candidates)
        try:
            response = await self._client.post(
                self._graphql_url,
                json={"query": query, "variables": {"diseaseId": disease_id}},
            )
            response.raise_for_status()
            payload = _EvidenceResponse.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            raise TargetCandidatesUnavailable(
                "Open Targets individual evidence lookup is unavailable"
            ) from error

        if payload.errors or payload.data is None or payload.data.disease is None:
            raise TargetCandidatesUnavailable(
                "Open Targets individual evidence lookup is unavailable"
            )

        collected: dict[str, dict[str, TargetCausalEvidence]] = {
            candidate.ensembl_id: {} for candidate in candidates
        }
        try:
            for alias, rows in payload.data.disease.items():
                expected_target, expected_axis = aliases[alias]
                for row in rows.rows:
                    if row.target.id != expected_target:
                        continue
                    evidence = self._causal_evidence(row, expected_axis, disease_id)
                    collected[expected_target][evidence.evidence_id] = evidence
        except (KeyError, ValidationError) as error:
            raise TargetCandidatesUnavailable(
                "Open Targets individual evidence response is invalid"
            ) from error
        return {target_id: tuple(items.values()) for target_id, items in collected.items()}

    @staticmethod
    def _evidence_query(
        candidates: tuple[TargetCandidate, ...],
    ) -> tuple[str, dict[str, tuple[str, CausalEvidenceAxis]]]:
        selections: list[str] = []
        aliases: dict[str, tuple[str, CausalEvidenceAxis]] = {}
        index = 0
        for candidate in candidates:
            for axis, datasource_ids in EVIDENCE_DATASOURCES.items():
                for datasource_id in datasource_ids:
                    alias = f"e{index}"
                    aliases[alias] = (candidate.ensembl_id, axis)
                    selections.append(
                        f'''{alias}: evidences(
                          ensemblIds: ["{candidate.ensembl_id}"]
                          enableIndirect: true
                          datasourceIds: ["{datasource_id}"]
                          size: {EVIDENCE_PER_DATASOURCE}
                        ) {{
                          rows {{
                            id datasourceId datatypeId score directionOnTrait directionOnTarget
                            targetRole confidence significantDriverMethods
                            disease {{ id name }}
                            target {{ id }}
                          }}
                        }}'''
                    )
                    index += 1
        query = (
            "query TargetCausalEvidence($diseaseId: String!) { "
            "disease(efoId: $diseaseId) { " + " ".join(selections) + " } }"
        )
        return query, aliases

    @staticmethod
    def _causal_evidence(
        row: _EvidenceRecord,
        axis: CausalEvidenceAxis,
        requested_disease_id: str,
    ) -> TargetCausalEvidence:
        target_directions = {
            "LoF": TargetEffectDirection.LOSS_OF_FUNCTION,
            "GoF": TargetEffectDirection.GAIN_OF_FUNCTION,
        }
        trait_directions = {
            "risk": TraitEffectDirection.RISK,
            "protect": TraitEffectDirection.PROTECTIVE,
            "protective": TraitEffectDirection.PROTECTIVE,
        }
        return TargetCausalEvidence(
            evidence_id=row.id,
            datasource_id=row.datasourceId,
            datatype_id=row.datatypeId,
            axis=axis,
            score=row.score,
            disease_id=row.disease.id,
            disease_name=row.disease.name,
            disease_scope=(
                DiseaseEvidenceScope.DIRECT
                if row.disease.id == requested_disease_id
                else DiseaseEvidenceScope.SUBTYPE
            ),
            direction_on_target=target_directions.get(
                row.directionOnTarget or "", TargetEffectDirection.UNKNOWN
            ),
            direction_on_trait=trait_directions.get(
                row.directionOnTrait or "", TraitEffectDirection.UNKNOWN
            ),
            target_role=row.targetRole,
            confidence=row.confidence,
            significant_driver_methods=tuple(row.significantDriverMethods),
        )

    async def _resolve_target(self, target_name: str) -> str:
        try:
            response = await self._client.post(
                self._graphql_url,
                json={
                    "query": TARGET_SEARCH_QUERY,
                    "variables": {"query": target_name, "size": 5},
                },
            )
            response.raise_for_status()
            payload = _TargetSearchResponse.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            raise TargetCandidatesUnavailable(
                "Open Targets target resolution is unavailable"
            ) from error

        if payload.errors or payload.data is None:
            raise TargetCandidatesUnavailable("Open Targets target resolution is unavailable")
        if not payload.data.search.hits:
            raise TargetCandidatesNotFound("specified target was not found")
        resolved_id = payload.data.search.hits[0].id
        if not resolved_id.startswith("ENSG"):
            raise TargetCandidatesNotFound("specified target did not resolve to an Ensembl gene")
        return resolved_id

    @staticmethod
    def _candidate(
        row: _AssociatedTarget,
    ) -> TargetCandidate | ExcludedTargetCandidate | None:
        reviewed = next(
            (
                protein.id
                for protein in row.target.proteinIds
                if protein.source == "uniprot_swissprot"
            ),
            None,
        )
        if reviewed is None:
            try:
                return ExcludedTargetCandidate(
                    ensembl_id=row.target.id,
                    approved_symbol=row.target.approvedSymbol,
                    association_score=row.score,
                    reason_code="reviewed_protein_unavailable",
                )
            except ValidationError:
                return None
        try:
            return TargetCandidate(
                ensembl_id=row.target.id,
                approved_symbol=row.target.approvedSymbol,
                approved_name=row.target.approvedName,
                biotype=row.target.biotype,
                function_descriptions=tuple(
                    description.strip()[:800]
                    for description in row.target.functionDescriptions[:2]
                    if description.strip()
                ),
                uniprot_accession=reviewed,
                association_score=row.score,
                data_type_scores=tuple(
                    TargetDataTypeScore(data_type=score.id, score=score.score)
                    for score in row.datatypeScores
                ),
                tractability_assessments=tuple(
                    TargetTractabilityAssessment(label=item.label, value=item.value)
                    for item in row.target.tractability
                    if item.modality == "SM"
                ),
            )
        except ValidationError:
            return None

    @staticmethod
    def _source_version(meta: _Meta) -> str:
        api = meta.apiVersion
        data = meta.dataVersion
        suffix = api.suffix or ""
        data_version = f"data-{data.year}.{data.month:02d}"
        if data.iteration is not None:
            data_version = f"{data_version}.{data.iteration}"
        return f"api-{api.x}.{api.y}.{api.z}{suffix}/{data_version}"


class _UniProtValue(BaseModel):
    value: str


class _UniProtRecommendedName(BaseModel):
    fullName: _UniProtValue


class _UniProtProteinDescription(BaseModel):
    recommendedName: _UniProtRecommendedName


class _UniProtGene(BaseModel):
    geneName: _UniProtValue


class _UniProtOrganism(BaseModel):
    taxonId: int


class _UniProtSequence(BaseModel):
    value: str


class _UniProtEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entryType: str
    primaryAccession: str
    uniProtkbId: str
    organism: _UniProtOrganism
    proteinDescription: _UniProtProteinDescription
    genes: list[_UniProtGene] = Field(min_length=1)
    sequence: _UniProtSequence


class UniProtProteinProvider:
    """accession으로 reviewed human canonical sequence를 검증해 반환한다."""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def get_reviewed_human_protein(self, accession: str) -> VerifiedProtein:
        try:
            response = await self._client.get(
                f"{self._base_url}/uniprotkb/{accession}.json",
                params={
                    "fields": "accession,id,gene_names,protein_name,sequence,length,organism_name"
                },
            )
            if response.status_code == 404:
                raise ProteinNotVerified("UniProt entry was not found")
            response.raise_for_status()
            entry = _UniProtEntry.model_validate(response.json())
        except ProteinNotVerified:
            raise
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            raise ProteinUnavailable("UniProt protein lookup is unavailable") from error

        if (
            entry.primaryAccession != accession
            or entry.entryType != "UniProtKB reviewed (Swiss-Prot)"
            or entry.organism.taxonId != 9606
        ):
            raise ProteinNotVerified("UniProt entry is not a reviewed human protein")

        try:
            return VerifiedProtein(
                accession=entry.primaryAccession,
                entry_name=entry.uniProtkbId,
                gene_symbol=entry.genes[0].geneName.value,
                protein_name=entry.proteinDescription.recommendedName.fullName.value,
                organism_taxon_id=entry.organism.taxonId,
                sequence=entry.sequence.value,
                retrieved_at=datetime.now(UTC),
            )
        except ValidationError as error:
            raise ProteinNotVerified(
                "UniProt sequence does not satisfy the DTA input contract"
            ) from error
