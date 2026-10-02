import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from evidrug_api.analysis_input.models import TargetMode
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.database import Base
from evidrug_api.dta.adapter import DtaProviderUnavailable, DtaToolAdapter
from evidrug_api.dta.contracts import (
    DtaArguments,
    DtaModel,
    DtaObservation,
    DtaResult,
    DtaScoreType,
)
from evidrug_api.dta.repository import DtaPersistenceConflict, DtaPersistenceError, DtaRepository
from evidrug_api.dta.service import DtaExecutionService
from evidrug_api.dta.tables import DtaExecutionRecord, DtaModelRecord, DtaObservationRecord

ARGUMENTS = DtaArguments(canonical_smiles="CCO", target_sequence="ACDEFGHIK")
MODEL = DtaModel(provider="test", model_id="synthetic", version="1", artifact_sha256="a" * 64)
OBSERVATION = DtaObservation(
    score_type=DtaScoreType.PREDICTED_PKD, value=5.0, unit="-log10(Kd [M])"
)


class Provider:
    model = MODEL

    def __init__(self, error: Exception | None = None, *, wait: bool = False) -> None:
        self.error = error
        self.wait = wait

    async def predict(self, arguments: DtaArguments) -> tuple[DtaObservation, ...]:
        assert arguments == ARGUMENTS
        if self.wait:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return (OBSERVATION,)


@pytest.mark.asyncio
async def test_adapter_preserves_prediction_and_isolates_expected_failure() -> None:
    result = await DtaToolAdapter(Provider()).execute(ARGUMENTS)
    assert result.status == "succeeded"
    assert result.observations == (OBSERVATION,)
    failure = await DtaToolAdapter(Provider(DtaProviderUnavailable("private details"))).execute(
        ARGUMENTS
    )
    assert failure.status == "unavailable"
    assert failure.observations == ()
    assert "private details" not in failure.model_dump_json()


@pytest.mark.asyncio
async def test_timeout_has_no_negative_prediction() -> None:
    result = await DtaToolAdapter(Provider(wait=True), timeout_seconds=0.01).execute(ARGUMENTS)
    assert result.error_code == "provider_timeout"
    assert result.observations == ()


@pytest.mark.asyncio
async def test_programming_errors_and_cancellation_propagate() -> None:
    with pytest.raises(ValueError, match="bug"):
        await DtaToolAdapter(Provider(ValueError("bug"))).execute(ARGUMENTS)
    task = asyncio.create_task(DtaToolAdapter(Provider(wait=True)).execute(ARGUMENTS))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("value", [True, "5.0", float("nan"), float("inf")])
def test_invalid_scores_are_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        DtaObservation.model_validate(
            {"score_type": "predicted_pkd", "value": value, "unit": "-log10(Kd [M])"}
        )


def test_score_units_probability_and_result_status_are_validated() -> None:
    with pytest.raises(ValidationError):
        DtaObservation(score_type=DtaScoreType.PREDICTED_PKD, value=5.0, unit="probability")
    with pytest.raises(ValidationError):
        DtaObservation(score_type=DtaScoreType.BINDING_PROBABILITY, value=1.1, unit="probability")
    with pytest.raises(ValidationError):
        DtaResult(
            model=MODEL,
            arguments=ARGUMENTS,
            status="unavailable",
            observations=(OBSERVATION,),
            duration_seconds=1,
        )
    with pytest.raises(ValidationError):
        DtaArguments(canonical_smiles="CCO", target_sequence="ACD\nEF")


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


async def new_analysis(session: AsyncSession) -> AnalysisRecord:
    record = AnalysisRecord(
        session_fingerprint="b" * 64,
        idempotency_key=str(uuid4()),
        disease_id="test",
        disease_name="Test",
        target_mode=TargetMode.DISCOVER,
        original_smiles="CCO",
        canonical_smiles="CCO",
    )
    session.add(record)
    await session.commit()
    return record


@pytest.mark.asyncio
async def test_sql_roundtrip_idempotency_and_shared_model(session: AsyncSession) -> None:
    analysis = await new_analysis(session)
    repository = DtaRepository(session)
    result = await DtaToolAdapter(Provider()).execute(ARGUMENTS)
    call_id, request_id, run_id = uuid4(), uuid4(), uuid4()
    ids = dict(tool_call_id=call_id, request_id=request_id, analysis_id=analysis.id, run_id=run_id)
    assert await repository.save(**ids, result=result, commit=True)
    assert not await repository.save(**ids, result=result, commit=True)
    session.expunge_all()
    assert await repository.load(call_id) == result
    await repository.save(
        tool_call_id=uuid4(),
        request_id=uuid4(),
        analysis_id=analysis.id,
        run_id=run_id,
        result=result,
    )
    assert await session.scalar(select(func.count()).select_from(DtaModelRecord)) == 1
    assert await session.scalar(select(func.count()).select_from(DtaObservationRecord)) == 2
    changed = result.model_copy(update={"duration_seconds": result.duration_seconds + 1})
    with pytest.raises(DtaPersistenceConflict):
        await repository.save(**ids, result=changed, commit=True)
    with pytest.raises(DtaPersistenceConflict):
        await repository.save(**(ids | {"tool_call_id": uuid4()}), result=result, commit=True)


@pytest.mark.asyncio
async def test_unavailable_is_stored_without_observations(session: AsyncSession) -> None:
    analysis = await new_analysis(session)
    result = await DtaToolAdapter(Provider(DtaProviderUnavailable())).execute(ARGUMENTS)
    repository = DtaRepository(session)
    call_id = uuid4()
    await repository.save(
        tool_call_id=call_id,
        request_id=uuid4(),
        analysis_id=analysis.id,
        run_id=uuid4(),
        result=result,
    )
    assert await repository.load(call_id) == result
    assert await session.scalar(select(func.count()).select_from(DtaObservationRecord)) == 0


@pytest.mark.asyncio
async def test_wrong_or_missing_analysis_leaves_no_result(session: AsyncSession) -> None:
    analysis = await new_analysis(session)
    result = await DtaToolAdapter(Provider()).execute(ARGUMENTS)
    repository = DtaRepository(session)
    ids = dict(tool_call_id=uuid4(), request_id=uuid4(), analysis_id=uuid4(), run_id=uuid4())
    with pytest.raises(DtaPersistenceError):
        await repository.save(**ids, result=result, commit=True)
    other = result.model_copy(
        update={"arguments": DtaArguments(canonical_smiles="CCC", target_sequence="ACD")}
    )
    with pytest.raises(DtaPersistenceConflict):
        await repository.save(**(ids | {"analysis_id": analysis.id}), result=other, commit=True)
    assert await session.scalar(select(func.count()).select_from(DtaModelRecord)) == 0


@pytest.mark.asyncio
async def test_failed_observation_insert_rolls_back_entire_result(session: AsyncSession) -> None:
    analysis = await new_analysis(session)
    analysis_id = analysis.id
    await session.execute(
        text(
            "CREATE TRIGGER reject_test_observation BEFORE INSERT ON dta_observations "
            "BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
    )
    await session.commit()
    result = await DtaToolAdapter(Provider()).execute(ARGUMENTS)
    repository = DtaRepository(session)
    with pytest.raises(DtaPersistenceError):
        await repository.save(
            tool_call_id=uuid4(),
            request_id=uuid4(),
            analysis_id=analysis_id,
            run_id=uuid4(),
            result=result,
        )
    for table in (DtaModelRecord, DtaExecutionRecord, DtaObservationRecord):
        assert await session.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.asyncio
async def test_different_score_types_roundtrip_without_combining(session: AsyncSession) -> None:
    analysis = await new_analysis(session)
    result = DtaResult(
        model=MODEL,
        arguments=ARGUMENTS,
        status="succeeded",
        duration_seconds=1,
        observations=(
            DtaObservation(score_type=DtaScoreType.PIC50_LIKE, value=4.2, unit="-log10(IC50 [M])"),
            DtaObservation(
                score_type=DtaScoreType.BINDING_PROBABILITY, value=0.8, unit="probability"
            ),
        ),
    )
    repository = DtaRepository(session)
    call_id = uuid4()
    ids = dict(tool_call_id=call_id, request_id=uuid4(), analysis_id=analysis.id, run_id=uuid4())
    await repository.save(**ids, result=result, commit=True)
    assert await repository.load(call_id) == result
    other_input = result.model_copy(
        update={"arguments": DtaArguments(canonical_smiles="CCO", target_sequence="ACD")}
    )
    with pytest.raises(DtaPersistenceConflict):
        await repository.save(**ids, result=other_input, commit=True)


@pytest.mark.asyncio
async def test_service_saves_prediction_and_does_not_recompute_completed_call(
    session: AsyncSession,
) -> None:
    analysis = await new_analysis(session)
    source = Provider()
    repository = DtaRepository(session)
    service = DtaExecutionService(DtaToolAdapter(source), repository)
    ids = dict(tool_call_id=uuid4(), request_id=uuid4(), analysis_id=analysis.id, run_id=uuid4())
    first = await service.execute(**ids, arguments=ARGUMENTS)
    source.error = ValueError("must not recompute")
    second = await service.execute(**ids, arguments=ARGUMENTS)
    assert first == second == await repository.load(ids["tool_call_id"])
    with pytest.raises(DtaPersistenceConflict):
        await service.execute(**(ids | {"run_id": uuid4()}), arguments=ARGUMENTS)
    with pytest.raises(DtaPersistenceConflict):
        await service.execute(
            **ids, arguments=DtaArguments(canonical_smiles="CCC", target_sequence="ACD")
        )


@pytest.mark.asyncio
async def test_service_keeps_unavailable_and_requires_new_id_for_retry(
    session: AsyncSession,
) -> None:
    analysis = await new_analysis(session)
    source = Provider(DtaProviderUnavailable())
    service = DtaExecutionService(DtaToolAdapter(source), DtaRepository(session))
    ids = dict(tool_call_id=uuid4(), request_id=uuid4(), analysis_id=analysis.id, run_id=uuid4())
    first = await service.execute(**ids, arguments=ARGUMENTS)
    assert first.status == "unavailable"
    source.error = None
    assert await service.execute(**ids, arguments=ARGUMENTS) == first
    retried = await service.execute(
        **(ids | {"tool_call_id": uuid4(), "request_id": uuid4()}), arguments=ARGUMENTS
    )
    assert retried.status == "succeeded"
