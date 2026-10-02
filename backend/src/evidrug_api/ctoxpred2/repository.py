"""CToxPred2 결과와 provenance를 원자적·멱등 저장한다."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.ctoxpred2.contracts import (
    CtoxChannel,
    CtoxChannelPrediction,
    CtoxModelIdentity,
    CtoxPredictionLabel,
    CtoxToolResult,
)
from evidrug_api.ctoxpred2.tables import (
    CtoxExecutionRecord,
    CtoxModelRecord,
    CtoxPredictionRecord,
)


class CtoxPersistenceConflict(RuntimeError):
    """기존 식별자의 불변 입력 또는 결과가 다른 경우."""


class CtoxPersistenceError(RuntimeError):
    """분석 부재 또는 저장 데이터 손상을 나타내는 공개 오류."""


class CtoxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def validate_input(self, analysis_id: UUID, canonical_smiles: str) -> None:
        stored = await self.session.scalar(
            select(AnalysisRecord.canonical_smiles).where(AnalysisRecord.id == analysis_id)
        )
        if stored is None:
            raise CtoxPersistenceError("analysis does not exist")
        if stored != canonical_smiles:
            raise CtoxPersistenceConflict("CToxPred2 input does not match analysis")

    async def _get(self, tool_call_id: UUID) -> CtoxExecutionRecord | None:
        return (
            await self.session.scalars(
                select(CtoxExecutionRecord)
                .where(CtoxExecutionRecord.tool_call_id == tool_call_id)
                .options(
                    selectinload(CtoxExecutionRecord.model),
                    selectinload(CtoxExecutionRecord.predictions),
                )
            )
        ).one_or_none()

    async def load(self, tool_call_id: UUID) -> CtoxToolResult | None:
        record = await self._get(tool_call_id)
        return None if record is None else self._result(record)

    async def save(
        self,
        *,
        tool_call_id: UUID,
        request_id: UUID,
        analysis_id: UUID,
        run_id: UUID,
        result: CtoxToolResult,
        commit: bool = True,
    ) -> bool:
        existing = await self._get(tool_call_id)
        if existing is not None:
            if (existing.request_id, existing.analysis_id, existing.run_id) != (
                request_id,
                analysis_id,
                run_id,
            ) or self._result(existing) != result:
                raise CtoxPersistenceConflict("tool call already has different CToxPred2 content")
            return False
        owner = await self.session.scalar(
            select(CtoxExecutionRecord.tool_call_id).where(
                CtoxExecutionRecord.request_id == request_id
            )
        )
        if owner is not None:
            raise CtoxPersistenceConflict("request already belongs to another tool call")
        await self.validate_input(analysis_id, result.canonical_smiles)
        identity_json = result.model.model_dump_json()
        model_key = hashlib.sha256(identity_json.encode()).hexdigest()
        model = await self.session.get(CtoxModelRecord, model_key)
        if model is None:
            model = CtoxModelRecord(id=model_key, identity_json=identity_json)
            self.session.add(model)
        elif CtoxModelIdentity.model_validate_json(model.identity_json) != result.model:
            raise CtoxPersistenceConflict("model hash already has different identity")
        self.session.add(
            CtoxExecutionRecord(
                tool_call_id=tool_call_id,
                request_id=request_id,
                analysis_id=analysis_id,
                run_id=run_id,
                model=model,
                canonical_smiles=result.canonical_smiles,
                limitations_json=json.dumps(result.limitations, ensure_ascii=False),
                predictions=[
                    CtoxPredictionRecord(
                        channel=item.channel.value,
                        position=position,
                        label=item.label.value,
                        class_probability=item.class_probability,
                    )
                    for position, item in enumerate(result.predictions)
                ],
            )
        )
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        return True

    @staticmethod
    def _result(record: CtoxExecutionRecord) -> CtoxToolResult:
        return CtoxToolResult(
            canonical_smiles=record.canonical_smiles,
            model=CtoxModelIdentity.model_validate_json(record.model.identity_json),
            predictions=tuple(
                CtoxChannelPrediction(
                    channel=CtoxChannel(item.channel),
                    label=CtoxPredictionLabel(item.label),
                    class_probability=item.class_probability,
                )
                for item in record.predictions
            ),
            limitations=tuple(json.loads(record.limitations_json)),
        )
