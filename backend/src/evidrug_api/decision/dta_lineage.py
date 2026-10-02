"""DTA Agent assay snapshot과 정규화 SQL 원장의 lineage를 검증한다."""

import hashlib
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.dta.agent import DtaAgentResult
from evidrug_api.dta.assay_repository import DtaAssayRepository
from evidrug_api.dta.evidence import AssaySource
from evidrug_api.execution_contracts.common import ContractModel
from evidrug_api.orchestration.upstream import InvalidUpstream


class DtaAssayEvidenceReference(ContractModel):
    ensembl_id: str
    source: AssaySource
    source_record_id: str
    source_tool_call_id: UUID


AssayEvidenceLineage = tuple[DtaAssayEvidenceReference, ...]

_PROVIDER_FAILURES = {
    "provider_timeout",
    "provider_unavailable",
    "provider_rate_limited",
    "provider_invalid_response",
}


async def verify_dta_assay_lineage(
    session: AsyncSession,
    *,
    analysis_id: UUID,
    run_id: UUID,
    canonical_smiles: str,
    result: DtaAgentResult,
) -> AssayEvidenceLineage:
    """새 snapshot은 SQL과 정확히 대조하고 assay_runs가 없는 구형 snapshot은 허용한다."""

    lineage: list[DtaAssayEvidenceReference] = []
    seen: set[tuple[str, AssaySource, str]] = set()
    repository = DtaAssayRepository(session)
    expected_smiles_hash = hashlib.sha256(canonical_smiles.encode()).hexdigest()
    for candidate in result.candidates:
        if not candidate.assay_runs:
            continue
        evidence_by_source = {
            source: tuple(item for item in candidate.experimental_evidence if item.source is source)
            for source in AssaySource
        }
        covered_sources: set[AssaySource] = set()
        for assay_run in candidate.assay_runs:
            record = await repository.get_record(assay_run.tool_call_id)
            # Admission 단계에서 거부된 호출은 provider SQL outcome을 만들지 않는다.
            if record is None and assay_run.error_code not in _PROVIDER_FAILURES:
                continue
            if record is None:
                raise InvalidUpstream("decision_dta_assay_query_missing")
            if (
                record.analysis_id != analysis_id
                or record.run_id != run_id
                or record.provider != assay_run.source.value
                or record.uniprot_accession != candidate.uniprot_accession
                or record.canonical_smiles_sha256 != expected_smiles_hash
                or record.status != assay_run.status
                or record.error_code != assay_run.error_code
                or record.external_requests != assay_run.external_requests
            ):
                raise InvalidUpstream("decision_dta_assay_query_mismatch")
            outcome = repository.outcome(record)
            if outcome.result is not None:
                if outcome.result.evidence != evidence_by_source[assay_run.source]:
                    raise InvalidUpstream("decision_dta_assay_evidence_mismatch")
                covered_sources.add(assay_run.source)
                for item in outcome.result.evidence:
                    key = (candidate.ensembl_id, item.source, item.source_record_id)
                    if key in seen:
                        raise InvalidUpstream("decision_dta_assay_lineage_duplicate")
                    seen.add(key)
                    lineage.append(
                        DtaAssayEvidenceReference(
                            ensembl_id=candidate.ensembl_id,
                            source=item.source,
                            source_record_id=item.source_record_id,
                            source_tool_call_id=assay_run.tool_call_id,
                        )
                    )
        if any(
            items and source not in covered_sources for source, items in evidence_by_source.items()
        ):
            raise InvalidUpstream("decision_dta_assay_lineage_incomplete")
    return tuple(lineage)
