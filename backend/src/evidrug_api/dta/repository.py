"""완료된 DTA 결과를 모델 정보와 관측 행으로 원자적·멱등 저장한다."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.dta.contracts import DtaModel, DtaResult
from evidrug_api.dta.tables import DtaExecutionRecord, DtaModelRecord, DtaObservationRecord


class DtaPersistenceConflict(RuntimeError):
    """같은 실행 식별자를 다른 입력·결과에 재사용했을 때 발생한다."""


class DtaPersistenceError(RuntimeError):
    """존재하지 않는 분석 또는 SQL 저장 실패를 공개 가능한 메시지로 표현한다."""


class DtaRepository:
    """호출 전용 session을 받아 완료 결과 저장 시 commit한다."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _get(self, tool_call_id: UUID) -> DtaExecutionRecord | None:
        records = await self.session.scalars(
            select(DtaExecutionRecord)
            .where(DtaExecutionRecord.tool_call_id == tool_call_id)
            .options(
                selectinload(DtaExecutionRecord.model),
                selectinload(DtaExecutionRecord.observations),
            )
        )
        return records.one_or_none()

    async def load(self, tool_call_id: UUID) -> DtaResult | None:
        """SQL column을 공개 결과 계약으로 복원한다."""
        record = await self._get(tool_call_id)
        return None if record is None else self._result(record)

    async def validate_input(self, analysis_id: UUID, canonical_smiles: str) -> None:
        """모델 실행 전에 분석 존재 여부와 분자 입력을 검증한다."""
        smiles = await self.session.scalar(
            select(AnalysisRecord.canonical_smiles).where(AnalysisRecord.id == analysis_id)
        )
        if smiles is None:
            raise DtaPersistenceError("analysis does not exist")
        if smiles != canonical_smiles:
            raise DtaPersistenceConflict("DTA input does not match analysis SMILES")

    async def save(
        self,
        *,
        tool_call_id: UUID,
        request_id: UUID,
        analysis_id: UUID,
        run_id: UUID,
        result: DtaResult,
        commit: bool = True,
    ) -> bool:
        """새 저장이면 True, 내용까지 동일한 재전달이면 False를 반환한다."""
        model_data = result.model.model_dump(mode="json")
        model_key = hashlib.sha256(
            json.dumps(model_data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for attempt in range(2):
            existing = await self._get(tool_call_id)
            if existing is not None:
                if (existing.request_id, existing.analysis_id, existing.run_id) != (
                    request_id,
                    analysis_id,
                    run_id,
                ) or self._result(existing) != result:
                    raise DtaPersistenceConflict("tool call already has different DTA content")
                return False
            owner = await self.session.scalar(
                select(DtaExecutionRecord.tool_call_id).where(
                    DtaExecutionRecord.request_id == request_id
                )
            )
            if owner is not None:
                raise DtaPersistenceConflict("request already belongs to another tool call")
            await self.validate_input(analysis_id, result.arguments.canonical_smiles)
            try:
                model = await self.session.get(DtaModelRecord, model_key)
                if model is None:
                    model = DtaModelRecord(id=model_key, **model_data)
                    self.session.add(model)
                elif self._model(model) != result.model:
                    raise DtaPersistenceConflict("model key already has different metadata")
                self.session.add(
                    DtaExecutionRecord(
                        tool_call_id=tool_call_id,
                        request_id=request_id,
                        analysis_id=analysis_id,
                        run_id=run_id,
                        model=model,
                        canonical_smiles=result.arguments.canonical_smiles,
                        target_sequence=result.arguments.target_sequence,
                        target_sequence_sha256=result.arguments.target_sequence_sha256,
                        status=result.status,
                        error_code=result.error_code,
                        duration_seconds=result.duration_seconds,
                        observations=[
                            DtaObservationRecord(
                                score_type=item.score_type.value,
                                value=item.value,
                                unit=item.unit,
                                position=position,
                            )
                            for position, item in enumerate(result.observations)
                        ],
                    )
                )
                if commit:
                    await self.session.commit()
                else:
                    await self.session.flush()
                return True
            except IntegrityError as error:
                if not commit:
                    raise
                await self.session.rollback()
                if attempt == 1:
                    raise DtaPersistenceError("failed to persist DTA result") from error
        raise AssertionError("unreachable")

    @staticmethod
    def _model(model: DtaModelRecord) -> DtaModel:
        return DtaModel(
            provider=model.provider,
            model_id=model.model_id,
            version=model.version,
            artifact_sha256=model.artifact_sha256,
        )

    @staticmethod
    def _result(record: DtaExecutionRecord) -> DtaResult:
        model = record.model
        result = DtaResult.model_validate(
            {
                "model": DtaRepository._model(model),
                "arguments": {
                    "canonical_smiles": record.canonical_smiles,
                    "target_sequence": record.target_sequence,
                },
                "status": record.status,
                "error_code": record.error_code,
                "duration_seconds": record.duration_seconds,
                "observations": [
                    {
                        "score_type": item.score_type,
                        "value": item.value,
                        "unit": item.unit,
                    }
                    for item in record.observations
                ],
            }
        )
        if result.arguments.target_sequence_sha256 != record.target_sequence_sha256:
            raise DtaPersistenceError("stored target sequence hash mismatch")
        return result
