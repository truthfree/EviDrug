import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_dta import ARGUMENTS, MODEL, OBSERVATION

from evidrug_api.analysis_input.models import TargetMode
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.database import Base
from evidrug_api.dta.adapter import DtaToolAdapter
from evidrug_api.dta.contracts import DtaArguments, DtaObservation
from evidrug_api.dta.repository import DtaRepository
from evidrug_api.dta.service import DtaExecutionService
from evidrug_api.dta.tables import DtaExecutionRecord
from evidrug_api.tool_execution.repository import (
    ExecutionAlreadyFailed,
    ExecutionInProgress,
    ExecutionLeaseLost,
    ExecutionRepository,
    database_now,
)
from evidrug_api.tool_execution.tables import ToolExecutionRecord


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], UUID]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tools.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        analysis = AnalysisRecord(
            session_fingerprint="a" * 64,
            idempotency_key="test",
            disease_id="test",
            disease_name="Test",
            target_mode=TargetMode.DISCOVER,
            original_smiles="CCO",
            canonical_smiles="CCO",
        )
        session.add(analysis)
        await session.commit()
        analysis_id = analysis.id
    try:
        yield factory, analysis_id
    finally:
        await engine.dispose()


class BlockingProvider:
    model = MODEL

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def predict(self, arguments: DtaArguments) -> tuple[DtaObservation, ...]:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return (OBSERVATION,)


def identifiers(analysis_id: UUID) -> dict[str, UUID]:
    return dict(tool_call_id=uuid4(), request_id=uuid4(), analysis_id=analysis_id, run_id=uuid4())


async def run(
    factory: async_sessionmaker[AsyncSession],
    source: BlockingProvider,
    ids: dict[str, UUID],
    *,
    timeout: float = 5,
) -> None:
    async with factory() as session:
        await DtaExecutionService(
            DtaToolAdapter(source), DtaRepository(session), timeout_seconds=timeout
        ).execute(**ids, arguments=ARGUMENTS)


@pytest.mark.asyncio
async def test_running_is_committed_before_inference_and_duplicate_does_not_execute(
    database: tuple[async_sessionmaker[AsyncSession], UUID],
) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    ids = identifiers(analysis_id)
    task = asyncio.create_task(run(factory, source, ids))
    try:
        await asyncio.wait_for(source.started.wait(), 5)
        async with factory() as observer:
            row = await observer.get(ToolExecutionRecord, ids["tool_call_id"])
            assert row is not None and row.status == "running"
        with pytest.raises(ExecutionInProgress):
            await run(factory, source, ids)
        assert source.calls == 1
    finally:
        source.release.set()
        await task
    async with factory() as observer:
        row = await observer.get(ToolExecutionRecord, ids["tool_call_id"])
        assert row is not None and row.status == "succeeded"
        assert await observer.get(DtaExecutionRecord, ids["tool_call_id"]) is not None
    await run(factory, source, ids)
    assert source.calls == 1


@pytest.mark.asyncio
async def test_recovery_fences_late_result_and_new_id_can_retry(
    database: tuple[async_sessionmaker[AsyncSession], UUID],
) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    ids = identifiers(analysis_id)
    task = asyncio.create_task(run(factory, source, ids))
    try:
        await asyncio.wait_for(source.started.wait(), 5)
        async with factory() as observer:
            expired = await database_now(observer) - timedelta(seconds=1)
            await observer.execute(update(ToolExecutionRecord).values(lease_expires_at=expired))
            await observer.commit()
            assert await ExecutionRepository(observer).recover_expired() == 1
            assert await ExecutionRepository(observer).recover_expired() == 0
        source.release.set()
        with pytest.raises(ExecutionLeaseLost):
            await task
        async with factory() as observer:
            row = await observer.get(ToolExecutionRecord, ids["tool_call_id"])
            assert row is not None and row.error_code == "lease_expired"
            assert await observer.get(DtaExecutionRecord, ids["tool_call_id"]) is None
        with pytest.raises(ExecutionAlreadyFailed):
            await run(factory, source, ids)
        await run(factory, source, identifiers(analysis_id))
        assert source.calls == 2
    finally:
        source.release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancellation_is_persisted(
    database: tuple[async_sessionmaker[AsyncSession], UUID], cancel: bool
) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    ids = identifiers(analysis_id)
    if cancel:
        task = asyncio.create_task(run(factory, source, ids))
        await asyncio.wait_for(source.started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(TimeoutError):
            await run(factory, source, ids, timeout=0.05)
    async with factory() as observer:
        row = await observer.get(ToolExecutionRecord, ids["tool_call_id"])
        assert row is not None and row.status == "failed"
        assert row.error_code == ("cancelled" if cancel else "provider_timeout")
    with pytest.raises(ExecutionAlreadyFailed):
        await run(factory, source, ids)
    assert source.calls == 1


@pytest.mark.asyncio
async def test_result_insert_failure_rolls_back_success_state(
    database: tuple[async_sessionmaker[AsyncSession], UUID],
) -> None:
    factory, analysis_id = database
    async with factory() as session:
        await session.execute(
            text(
                "CREATE TRIGGER reject_observation BEFORE INSERT ON dta_observations "
                "BEGIN SELECT RAISE(ABORT, 'test'); END"
            )
        )
        await session.commit()
    source = BlockingProvider()
    source.release.set()
    ids = identifiers(analysis_id)
    with pytest.raises(IntegrityError):
        await run(factory, source, ids)
    async with factory() as observer:
        row = await observer.get(ToolExecutionRecord, ids["tool_call_id"])
        assert row is not None and row.status == "failed"
        assert row.error_code == "persistence_failed"
        assert await observer.scalar(select(func.count()).select_from(DtaExecutionRecord)) == 0


@pytest.mark.asyncio
async def test_simultaneous_claims_have_only_one_inference_owner(
    database: tuple[async_sessionmaker[AsyncSession], UUID],
) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    ids = identifiers(analysis_id)
    tasks = [asyncio.create_task(run(factory, source, ids)) for _ in range(2)]
    try:
        done, pending = await asyncio.wait(tasks, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        assert len(done) == 1 and len(pending) == 1
        assert isinstance(next(iter(done)).exception(), ExecutionInProgress)
        await asyncio.wait_for(source.started.wait(), 5)
        assert source.calls == 1
    finally:
        source.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(task.exception() is None for task in tasks) == 1


@pytest.mark.asyncio
async def test_recovery_entrypoint_uses_independent_engine(
    database: tuple[async_sessionmaker[AsyncSession], UUID],
) -> None:
    from evidrug_api.tool_execution.recover import recover_expired_executions
    from evidrug_api.tool_execution.repository import ExecutionIdentity

    factory, analysis_id = database
    async with factory() as session:
        identity = ExecutionIdentity(
            **identifiers(analysis_id), tool_id="dta", input_sha256="a" * 64
        )
        await ExecutionRepository(session).claim(identity, lease_seconds=300)
        expired = await database_now(session) - timedelta(seconds=1)
        await session.execute(update(ToolExecutionRecord).values(lease_expires_at=expired))
        await session.commit()
        url = str(session.bind.url)  # type: ignore[union-attr]
    assert await recover_expired_executions(url) == 1
    assert await recover_expired_executions(url) == 0
