"""외부 assay 조회 outcome을 원자적·멱등 저장하고 재구성한다."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.dta.assay_tables import DtaAssayEvidenceRecord, DtaAssayQueryRecord
from evidrug_api.dta.evidence import (
    AssaySource,
    DtaAssayArguments,
    DtaAssayResult,
)


class DtaAssayPersistenceConflict(RuntimeError):
    """같은 실행 식별자가 다른 조회를 가리킬 때 발생한다."""


class DtaAssayPersistenceError(RuntimeError):
    """분석 부재 또는 SQL 저장 실패를 공개 가능한 경계로 표현한다."""


@dataclass(frozen=True)
class StoredDtaAssayOutcome:
    """성공 결과와 실패 tombstone을 함께 표현하는 저장 결과."""

    status: str
    result: DtaAssayResult | None
    error_code: str | None
    external_requests: int


class DtaAssayRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_record(self, tool_call_id: UUID) -> DtaAssayQueryRecord | None:
        """lineage 검증에 필요한 저장 query와 evidence를 함께 읽는다."""
        records = await self.session.scalars(
            select(DtaAssayQueryRecord)
            .where(DtaAssayQueryRecord.tool_call_id == tool_call_id)
            .options(selectinload(DtaAssayQueryRecord.evidence))
        )
        return records.one_or_none()

    async def load(
        self,
        *,
        tool_call_id: UUID,
        request_id: UUID,
        analysis_id: UUID,
        run_id: UUID,
        provider: AssaySource,
        provider_version: str,
        arguments: DtaAssayArguments,
    ) -> StoredDtaAssayOutcome | None:
        record = await self.get_record(tool_call_id)
        if record is None:
            return None
        expected = (
            request_id,
            analysis_id,
            run_id,
            provider.value,
            provider_version,
            self._smiles_hash(arguments.canonical_smiles),
            arguments.uniprot_accession,
        )
        actual = (
            record.request_id,
            record.analysis_id,
            record.run_id,
            record.provider,
            record.provider_version,
            record.canonical_smiles_sha256,
            record.uniprot_accession,
        )
        if actual != expected:
            raise DtaAssayPersistenceConflict("tool call already has different assay content")
        return self.outcome(record)

    async def validate_input(self, analysis_id: UUID, canonical_smiles: str) -> None:
        smiles = await self.session.scalar(
            select(AnalysisRecord.canonical_smiles).where(AnalysisRecord.id == analysis_id)
        )
        if smiles is None:
            raise DtaAssayPersistenceError("analysis does not exist")
        if smiles != canonical_smiles:
            raise DtaAssayPersistenceConflict("assay input does not match analysis SMILES")

    async def save(
        self,
        *,
        tool_call_id: UUID,
        request_id: UUID,
        analysis_id: UUID,
        run_id: UUID,
        provider: AssaySource,
        provider_version: str,
        arguments: DtaAssayArguments,
        status: Literal["succeeded", "no_records", "failed"],
        error_code: str | None,
        external_requests: int,
        started_at: datetime,
        finished_at: datetime,
        duration_ms: int,
        result: DtaAssayResult | None,
    ) -> bool:
        self._validate_outcome(
            provider=provider,
            status=status,
            error_code=error_code,
            external_requests=external_requests,
            result=result,
        )
        for attempt in range(2):
            existing = await self.load(
                tool_call_id=tool_call_id,
                request_id=request_id,
                analysis_id=analysis_id,
                run_id=run_id,
                provider=provider,
                provider_version=provider_version,
                arguments=arguments,
            )
            if existing is not None:
                expected = StoredDtaAssayOutcome(
                    status=status,
                    result=result,
                    error_code=error_code,
                    external_requests=external_requests,
                )
                if existing != expected:
                    raise DtaAssayPersistenceConflict(
                        "tool call already has different assay outcome"
                    )
                return False
            owner = await self.session.scalar(
                select(DtaAssayQueryRecord.tool_call_id).where(
                    DtaAssayQueryRecord.request_id == request_id
                )
            )
            if owner is not None:
                raise DtaAssayPersistenceConflict("request belongs to another assay tool call")
            await self.validate_input(analysis_id, arguments.canonical_smiles)
            evidence = () if result is None else result.evidence
            try:
                self.session.add(
                    DtaAssayQueryRecord(
                        tool_call_id=tool_call_id,
                        request_id=request_id,
                        analysis_id=analysis_id,
                        run_id=run_id,
                        provider=provider.value,
                        provider_version=provider_version,
                        canonical_smiles_sha256=self._smiles_hash(arguments.canonical_smiles),
                        uniprot_accession=arguments.uniprot_accession,
                        status=status,
                        error_code=error_code,
                        external_requests=external_requests,
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_ms=duration_ms,
                        evidence=[
                            DtaAssayEvidenceRecord(
                                position=position,
                                source_record_id=item.source_record_id,
                                kind=item.kind.value,
                                endpoint=None if item.endpoint is None else item.endpoint.value,
                                value=item.value,
                                unit=item.unit,
                                qualitative_outcome=item.qualitative_outcome,
                                assay_description=item.assay_description,
                                doi=item.doi,
                                pmid=item.pmid,
                            )
                            for position, item in enumerate(evidence)
                        ],
                    )
                )
                await self.session.commit()
                return True
            except IntegrityError as error:
                await self.session.rollback()
                if attempt == 1:
                    raise DtaAssayPersistenceError("failed to persist assay outcome") from error
        raise AssertionError("unreachable")

    @staticmethod
    def _smiles_hash(canonical_smiles: str) -> str:
        return hashlib.sha256(canonical_smiles.encode()).hexdigest()

    @staticmethod
    def _validate_outcome(
        *,
        provider: AssaySource,
        status: str,
        error_code: str | None,
        external_requests: int,
        result: DtaAssayResult | None,
    ) -> None:
        valid_failure = status == "failed" and error_code is not None and result is None
        valid_result = (
            status in {"succeeded", "no_records"}
            and error_code is None
            and result is not None
            and result.status == status
            and result.source is provider
            and result.external_requests == external_requests
        )
        if external_requests < 0 or not (valid_failure or valid_result):
            raise DtaAssayPersistenceConflict("invalid assay outcome")

    @staticmethod
    def outcome(record: DtaAssayQueryRecord) -> StoredDtaAssayOutcome:
        """저장 query를 공개 assay outcome으로 복원한다."""
        if record.status == "failed":
            return StoredDtaAssayOutcome(
                status=record.status,
                result=None,
                error_code=record.error_code,
                external_requests=record.external_requests,
            )
        source = AssaySource(record.provider)
        result = DtaAssayResult.model_validate(
            {
                "source": source,
                "status": record.status,
                "evidence": [
                    {
                        "source": source,
                        "source_record_id": item.source_record_id,
                        "kind": item.kind,
                        "endpoint": item.endpoint,
                        "value": item.value,
                        "unit": item.unit,
                        "qualitative_outcome": item.qualitative_outcome,
                        "assay_description": item.assay_description,
                        "doi": item.doi,
                        "pmid": item.pmid,
                    }
                    for item in record.evidence
                ],
                "external_requests": record.external_requests,
            }
        )
        return StoredDtaAssayOutcome(
            status=record.status,
            result=result,
            error_code=None,
            external_requests=record.external_requests,
        )
