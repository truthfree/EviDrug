from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_ctoxpred2_adapter import provider_report

from evidrug_api.analysis_input.models import TargetMode
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.ctoxpred2.adapter import CtoxToolAdapter
from evidrug_api.ctoxpred2.contracts import CtoxToolArguments
from evidrug_api.ctoxpred2.repository import CtoxPersistenceConflict, CtoxRepository
from evidrug_api.ctoxpred2.service import CtoxExecutionService
from evidrug_api.database import Base


class Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def predict(self, canonical_smiles: str) -> dict[str, object]:
        self.calls += 1
        report = provider_report()
        report["canonical_smiles"] = canonical_smiles
        return report


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


async def analysis(session: AsyncSession) -> AnalysisRecord:
    record = AnalysisRecord(
        session_fingerprint="c" * 64,
        idempotency_key=str(uuid4()),
        disease_id="EFO_0000305",
        disease_name="breast cancer",
        target_mode=TargetMode.DISCOVER,
        original_smiles="CCO",
        canonical_smiles="CCO",
    )
    session.add(record)
    await session.commit()
    return record


@pytest.mark.asyncio
async def test_service_roundtrip_and_completed_call_does_not_recompute(
    session: AsyncSession,
) -> None:
    case = await analysis(session)
    provider = Provider()
    repository = CtoxRepository(session)
    service = CtoxExecutionService(CtoxToolAdapter(provider), repository)
    ids = dict(tool_call_id=uuid4(), request_id=uuid4(), analysis_id=case.id, run_id=uuid4())
    arguments = CtoxToolArguments(canonical_smiles="CCO")

    first = await service.execute(**ids, arguments=arguments)
    second = await service.execute(**ids, arguments=arguments)

    assert first == second == await repository.load(ids["tool_call_id"])
    assert provider.calls == 1
    assert len(first.predictions) == 3


@pytest.mark.asyncio
async def test_service_rejects_wrong_analysis_input(session: AsyncSession) -> None:
    case = await analysis(session)
    service = CtoxExecutionService(CtoxToolAdapter(Provider()), CtoxRepository(session))
    with pytest.raises(CtoxPersistenceConflict):
        await service.execute(
            tool_call_id=uuid4(),
            request_id=uuid4(),
            analysis_id=case.id,
            run_id=uuid4(),
            arguments=CtoxToolArguments(canonical_smiles="CCC"),
        )
