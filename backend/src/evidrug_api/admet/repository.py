"""ADMET manifest와 실행별 prediction을 원자적으로 저장·조회한다."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import Select

from evidrug_api.admet.contracts import (
    AdmetEndpointDefinition,
    AdmetEndpointPrediction,
    AdmetModelArtifact,
    AdmetModelManifest,
    AdmetNormalizedOutput,
    AdmetToolResult,
)
from evidrug_api.admet.tables import (
    AdmetEndpointRecord,
    AdmetManifestLimitationRecord,
    AdmetModelArtifactRecord,
    AdmetModelManifestRecord,
    AdmetPredictionRecord,
    AdmetToolExecutionRecord,
)
from evidrug_api.analysis_jobs.tables import AnalysisRecord


class AdmetPersistenceConflict(RuntimeError):
    """같은 식별자가 이미 다른 불변 내용으로 저장된 경우."""


class AdmetPersistenceError(RuntimeError):
    """공개 가능한 메시지로 정규화한 ADMET transaction 실패."""


class AdmetRepository:
    """stable manifest 재사용과 tool call 멱등 저장을 관리한다."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_execution(self, tool_call_id: UUID) -> AdmetToolExecutionRecord | None:
        """manifest와 endpoint를 포함한 한 ADMET 실행을 조회한다."""

        statement = self._loaded_execution().where(
            AdmetToolExecutionRecord.tool_call_id == tool_call_id
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def load_output(self, tool_call_id: UUID) -> AdmetNormalizedOutput | None:
        """저장된 SQL 행을 원래 Pydantic 계약으로 재구성한다."""

        execution = await self.get_execution(tool_call_id)
        return None if execution is None else self._to_output(execution)

    async def save(
        self,
        *,
        tool_call_id: UUID,
        request_id: UUID,
        analysis_id: UUID,
        run_id: UUID,
        output: AdmetNormalizedOutput,
        commit: bool = True,
    ) -> tuple[AdmetToolExecutionRecord, bool]:
        """manifest와 실행 결과를 하나의 transaction으로 멱등 저장한다."""

        existing = await self.get_execution(tool_call_id)
        if existing is not None:
            self._validate_duplicate_execution(
                existing,
                request_id=request_id,
                analysis_id=analysis_id,
                run_id=run_id,
                output=output,
            )
            return existing, False

        request_owner = await self._get_execution_by_request(request_id)
        if request_owner is not None:
            raise AdmetPersistenceConflict("request_id already belongs to another tool call")

        analysis_smiles = await self.session.scalar(
            select(AnalysisRecord.canonical_smiles).where(AnalysisRecord.id == analysis_id)
        )
        if analysis_smiles is None:
            raise AdmetPersistenceError("analysis does not exist")
        if analysis_smiles != output.result.canonical_smiles:
            raise AdmetPersistenceConflict("ADMET output SMILES does not match the analysis")

        for attempt in range(2):
            try:
                execution = await self._insert_execution(
                    tool_call_id=tool_call_id,
                    request_id=request_id,
                    analysis_id=analysis_id,
                    run_id=run_id,
                    output=output,
                )
                if commit:
                    await self.session.commit()
                created = await self.get_execution(execution.tool_call_id)
                if created is None:  # pragma: no cover - database invariant
                    raise RuntimeError("persisted ADMET execution could not be reloaded")
                return created, True
            except IntegrityError as error:
                if not commit:
                    raise
                await self.session.rollback()
                duplicate = await self.get_execution(tool_call_id)
                if duplicate is not None:
                    self._validate_duplicate_execution(
                        duplicate,
                        request_id=request_id,
                        analysis_id=analysis_id,
                        run_id=run_id,
                        output=output,
                    )
                    return duplicate, False
                request_owner = await self._get_execution_by_request(request_id)
                if request_owner is not None:
                    raise AdmetPersistenceConflict(
                        "request_id already belongs to another tool call"
                    ) from error
                if attempt == 1:
                    raise AdmetPersistenceError("failed to persist ADMET execution") from error

        raise RuntimeError("ADMET persistence retry loop exited unexpectedly")  # pragma: no cover

    async def _insert_execution(
        self,
        *,
        tool_call_id: UUID,
        request_id: UUID,
        analysis_id: UUID,
        run_id: UUID,
        output: AdmetNormalizedOutput,
    ) -> AdmetToolExecutionRecord:
        manifest = await self._get_manifest(output.manifest.manifest_sha256)
        if manifest is None:
            manifest = self._new_manifest(output.manifest)
            self.session.add(manifest)
            await self.session.flush()
        elif self._manifest_to_contract(manifest) != output.manifest:
            raise AdmetPersistenceConflict("manifest hash already has different content")

        endpoints_by_key = {endpoint.endpoint_id: endpoint for endpoint in manifest.endpoints}
        execution = AdmetToolExecutionRecord(
            tool_call_id=tool_call_id,
            request_id=request_id,
            analysis_id=analysis_id,
            run_id=run_id,
            manifest_id=manifest.id,
            canonical_smiles=output.result.canonical_smiles,
        )
        execution.predictions = [
            AdmetPredictionRecord(
                manifest_id=manifest.id,
                endpoint_record_id=endpoints_by_key[prediction.endpoint_id].id,
                position=position,
                value=prediction.value,
                drugbank_approved_percentile=prediction.drugbank_approved_percentile,
            )
            for position, prediction in enumerate(output.result.predictions)
        ]
        self.session.add(execution)
        await self.session.flush()
        return execution

    async def _get_manifest(self, manifest_sha256: str) -> AdmetModelManifestRecord | None:
        statement = self._loaded_manifest().where(
            AdmetModelManifestRecord.manifest_sha256 == manifest_sha256
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def _get_execution_by_request(self, request_id: UUID) -> AdmetToolExecutionRecord | None:
        statement = self._loaded_execution().where(
            AdmetToolExecutionRecord.request_id == request_id
        )
        return (await self.session.scalars(statement)).one_or_none()

    @staticmethod
    def _new_manifest(manifest: AdmetModelManifest) -> AdmetModelManifestRecord:
        return AdmetModelManifestRecord(
            manifest_sha256=manifest.manifest_sha256,
            schema_version=manifest.schema_version,
            tool_id=manifest.tool_id,
            tool_version=manifest.tool_version,
            endpoint_metadata_sha256=manifest.endpoint_metadata_sha256,
            reference_population=manifest.reference_population,
            reference_sha256=manifest.reference_sha256,
            model_artifacts=[
                AdmetModelArtifactRecord(
                    relative_path=artifact.relative_path,
                    position=position,
                    sha256=artifact.sha256,
                )
                for position, artifact in enumerate(manifest.model_artifacts)
            ],
            endpoints=[
                AdmetEndpointRecord(
                    endpoint_id=endpoint.endpoint_id,
                    position=position,
                    category=endpoint.category,
                    name=endpoint.name,
                    task_type=endpoint.task_type,
                    dataset_size=endpoint.dataset_size,
                    units=endpoint.units,
                    minimum=endpoint.minimum,
                    maximum=endpoint.maximum,
                    minimum_unbounded=endpoint.minimum_unbounded,
                    maximum_unbounded=endpoint.maximum_unbounded,
                    species=endpoint.species,
                    tdc_rank=endpoint.tdc_rank,
                    auprc=endpoint.auprc,
                    auroc=endpoint.auroc,
                    r_squared=endpoint.r_squared,
                    mae=endpoint.mae,
                    source_url=endpoint.source_url,
                )
                for position, endpoint in enumerate(manifest.endpoints)
            ],
            limitations=[
                AdmetManifestLimitationRecord(position=position, limitation=limitation)
                for position, limitation in enumerate(manifest.limitations)
            ],
        )

    @staticmethod
    def _to_output(execution: AdmetToolExecutionRecord) -> AdmetNormalizedOutput:
        manifest = AdmetRepository._manifest_to_contract(execution.manifest)
        result = AdmetToolResult(
            manifest_sha256=manifest.manifest_sha256,
            canonical_smiles=execution.canonical_smiles,
            predictions=tuple(
                AdmetEndpointPrediction(
                    endpoint_id=prediction.endpoint.endpoint_id,
                    value=prediction.value,
                    drugbank_approved_percentile=prediction.drugbank_approved_percentile,
                )
                for prediction in execution.predictions
            ),
        )
        return AdmetNormalizedOutput(manifest=manifest, result=result)

    @staticmethod
    def _manifest_to_contract(record: AdmetModelManifestRecord) -> AdmetModelManifest:
        if record.schema_version != "1":
            raise AdmetPersistenceError("stored manifest has an unsupported schema version")
        if record.tool_id != "admet_ai":
            raise AdmetPersistenceError("stored manifest has an unsupported tool identifier")
        return AdmetModelManifest(
            schema_version="1",
            manifest_sha256=record.manifest_sha256,
            tool_id="admet_ai",
            tool_version=record.tool_version,
            endpoint_metadata_sha256=record.endpoint_metadata_sha256,
            reference_population=record.reference_population,
            reference_sha256=record.reference_sha256,
            model_artifacts=tuple(
                AdmetModelArtifact(relative_path=item.relative_path, sha256=item.sha256)
                for item in record.model_artifacts
            ),
            endpoints=tuple(
                AdmetEndpointDefinition(
                    endpoint_id=endpoint.endpoint_id,
                    category=endpoint.category,
                    name=endpoint.name,
                    task_type=endpoint.task_type,
                    dataset_size=endpoint.dataset_size,
                    units=endpoint.units,
                    minimum=endpoint.minimum,
                    maximum=endpoint.maximum,
                    minimum_unbounded=endpoint.minimum_unbounded,
                    maximum_unbounded=endpoint.maximum_unbounded,
                    species=endpoint.species,
                    tdc_rank=endpoint.tdc_rank,
                    auprc=endpoint.auprc,
                    auroc=endpoint.auroc,
                    r_squared=endpoint.r_squared,
                    mae=endpoint.mae,
                    source_url=endpoint.source_url,
                )
                for endpoint in record.endpoints
            ),
            limitations=tuple(item.limitation for item in record.limitations),
        )

    def _validate_duplicate_execution(
        self,
        execution: AdmetToolExecutionRecord,
        *,
        request_id: UUID,
        analysis_id: UUID,
        run_id: UUID,
        output: AdmetNormalizedOutput,
    ) -> None:
        if (
            execution.request_id != request_id
            or execution.analysis_id != analysis_id
            or execution.run_id != run_id
            or self._to_output(execution) != output
        ):
            raise AdmetPersistenceConflict("tool_call_id already has different ADMET content")

    @staticmethod
    def _loaded_manifest() -> Select[tuple[AdmetModelManifestRecord]]:
        return select(AdmetModelManifestRecord).options(
            selectinload(AdmetModelManifestRecord.model_artifacts),
            selectinload(AdmetModelManifestRecord.endpoints),
            selectinload(AdmetModelManifestRecord.limitations),
        )

    @staticmethod
    def _loaded_execution() -> Select[tuple[AdmetToolExecutionRecord]]:
        return select(AdmetToolExecutionRecord).options(
            selectinload(AdmetToolExecutionRecord.manifest).selectinload(
                AdmetModelManifestRecord.model_artifacts
            ),
            selectinload(AdmetToolExecutionRecord.manifest).selectinload(
                AdmetModelManifestRecord.endpoints
            ),
            selectinload(AdmetToolExecutionRecord.manifest).selectinload(
                AdmetModelManifestRecord.limitations
            ),
            selectinload(AdmetToolExecutionRecord.predictions).selectinload(
                AdmetPredictionRecord.endpoint
            ),
        )
