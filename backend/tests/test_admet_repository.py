from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from test_admet_adapter import provider_report

from evidrug_api.admet.adapter import AdmetToolAdapter
from evidrug_api.admet.contracts import (
    AdmetEndpointDefinition,
    AdmetEndpointPrediction,
    AdmetModelArtifact,
    AdmetModelManifest,
    AdmetNormalizedOutput,
    AdmetTaskType,
    AdmetToolArguments,
    AdmetToolResult,
)
from evidrug_api.admet.provider import AdmetProviderUnavailable
from evidrug_api.admet.repository import (
    AdmetPersistenceConflict,
    AdmetPersistenceError,
    AdmetRepository,
)
from evidrug_api.admet.service import AdmetExecutionService
from evidrug_api.admet.tables import (
    AdmetEndpointRecord,
    AdmetModelManifestRecord,
    AdmetPredictionRecord,
    AdmetToolExecutionRecord,
)
from evidrug_api.analysis_input.models import TargetMode
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.database import Base
from evidrug_api.tool_execution.repository import ExecutionAlreadyFailed
from evidrug_api.tool_execution.tables import ToolExecutionRecord


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def predict(self, canonical_smiles: str) -> dict[str, object]:
        self.calls += 1
        if self.fail:
            raise AdmetProviderUnavailable("provider_unavailable")
        report = provider_report()
        report["smiles"] = canonical_smiles
        return report


@pytest.mark.asyncio
async def test_service_reuses_saved_output_without_recomputing(
    database_session: AsyncSession,
) -> None:
    analysis_id = await create_analysis(database_session)
    source = CountingProvider()
    service = AdmetExecutionService(AdmetToolAdapter(source), AdmetRepository(database_session))
    ids = dict(tool_call_id=uuid4(), request_id=uuid4(), analysis_id=analysis_id, run_id=uuid4())
    arguments = AdmetToolArguments(canonical_smiles="CCO")
    first = await service.execute(**ids, arguments=arguments)
    assert await service.execute(**ids, arguments=arguments) == first
    assert source.calls == 1
    with pytest.raises(AdmetPersistenceConflict):
        await service.execute(**(ids | {"run_id": uuid4()}), arguments=arguments)
    with pytest.raises(AdmetPersistenceConflict):
        await service.execute(**ids, arguments=AdmetToolArguments(canonical_smiles="CCC"))
    assert source.calls == 1


@pytest.mark.asyncio
async def test_service_prevalidates_analysis_and_does_not_store_provider_failure(
    database_session: AsyncSession,
) -> None:
    analysis_id = await create_analysis(database_session)
    source = CountingProvider()
    source.fail = True
    service = AdmetExecutionService(AdmetToolAdapter(source), AdmetRepository(database_session))
    ids = dict(tool_call_id=uuid4(), request_id=uuid4(), analysis_id=analysis_id, run_id=uuid4())
    with pytest.raises(AdmetPersistenceConflict):
        await service.execute(**ids, arguments=AdmetToolArguments(canonical_smiles="CCC"))
    assert source.calls == 0
    with pytest.raises(AdmetProviderUnavailable):
        await service.execute(**ids, arguments=AdmetToolArguments(canonical_smiles="CCO"))
    ledger = await database_session.get(ToolExecutionRecord, ids["tool_call_id"])
    assert ledger is not None and ledger.status == "failed"
    assert ledger.error_code == "provider_unavailable"
    with pytest.raises(ExecutionAlreadyFailed):
        await service.execute(**ids, arguments=AdmetToolArguments(canonical_smiles="CCO"))
    assert source.calls == 1
    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetToolExecutionRecord))
        == 0
    )


@pytest_asyncio.fixture
async def database_session() -> AsyncIterator[AsyncSession]:
    """foreign key가 활성화된 격리 SQLite 세션을 제공한다."""

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


def admet_output(*, hash_prefix: str = "a", smiles: str = "CCO") -> AdmetNormalizedOutput:
    """두 endpoint를 가진 작고 결정적인 정규화 결과를 만든다."""

    manifest_sha256 = hash_prefix * 64
    manifest = AdmetModelManifest(
        manifest_sha256=manifest_sha256,
        tool_version="1.4.0",
        endpoint_metadata_sha256="b" * 64,
        reference_population="DrugBank approved 1.0",
        reference_sha256="c" * 64,
        model_artifacts=(AdmetModelArtifact(relative_path="models/0.pt", sha256="d" * 64),),
        endpoints=(
            AdmetEndpointDefinition(
                endpoint_id="AMES",
                category="Toxicity",
                name="Mutagenicity",
                task_type=AdmetTaskType.CLASSIFICATION,
                dataset_size=7255,
                units=None,
                minimum=0,
                maximum=1,
                species="salmonella typhimurium",
                tdc_rank=2,
                auprc=0.91,
                auroc=0.89,
                source_url="https://tdcommons.ai/single_pred_tasks/tox/#ames-mutagenicity",
            ),
            AdmetEndpointDefinition(
                endpoint_id="Caco2_Wang",
                category="Absorption",
                name="Caco-2 permeability",
                task_type=AdmetTaskType.REGRESSION,
                dataset_size=906,
                units="log cm/s",
                minimum=-7.76,
                maximum=-3.51,
                species="human",
                tdc_rank=1,
                r_squared=0.67,
                mae=0.31,
                source_url="https://tdcommons.ai/single_pred_tasks/adme/#caco2-wang",
            ),
        ),
        limitations=("Percentile is a reference rank, not model confidence.",),
    )
    return AdmetNormalizedOutput(
        manifest=manifest,
        result=AdmetToolResult(
            manifest_sha256=manifest_sha256,
            canonical_smiles=smiles,
            predictions=(
                AdmetEndpointPrediction(
                    endpoint_id="AMES",
                    value=0.21,
                    drugbank_approved_percentile=32.5,
                ),
                AdmetEndpointPrediction(
                    endpoint_id="Caco2_Wang",
                    value=-4.71,
                    drugbank_approved_percentile=64.0,
                ),
            ),
        ),
    )


async def create_analysis(session: AsyncSession, *, smiles: str = "CCO") -> UUID:
    """ADMET 실행이 참조할 최소 분석 행을 만든다."""

    analysis = AnalysisRecord(
        session_fingerprint="f" * 64,
        idempotency_key=str(uuid4()),
        disease_id="MONDO_0004975",
        disease_name="Alzheimer disease",
        target_mode=TargetMode.DISCOVER,
        target_name=None,
        original_smiles=smiles,
        canonical_smiles=smiles,
    )
    session.add(analysis)
    await session.commit()
    return analysis.id


async def persist_output(
    session: AsyncSession,
    analysis_id: UUID,
    output: AdmetNormalizedOutput,
    *,
    tool_call_id: UUID | None = None,
) -> tuple[UUID, UUID, UUID]:
    """새 실행을 저장하고 호출 식별자들을 반환한다."""

    call_id = tool_call_id or uuid4()
    request_id = uuid4()
    run_id = uuid4()
    _, created = await AdmetRepository(session).save(
        tool_call_id=call_id,
        request_id=request_id,
        analysis_id=analysis_id,
        run_id=run_id,
        output=output,
    )
    assert created is True
    return call_id, request_id, run_id


@pytest.mark.asyncio
async def test_save_round_trips_contract_without_repeating_endpoint_metadata(
    database_session: AsyncSession,
) -> None:
    analysis_id = await create_analysis(database_session)
    output = admet_output()
    tool_call_id, _, _ = await persist_output(database_session, analysis_id, output)

    loaded = await AdmetRepository(database_session).load_output(tool_call_id)

    assert loaded == output
    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetModelManifestRecord))
        == 1
    )
    assert await database_session.scalar(select(func.count()).select_from(AdmetEndpointRecord)) == 2
    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetPredictionRecord)) == 2
    )
    assert set(AdmetPredictionRecord.__table__.columns.keys()) == {
        "tool_call_id",
        "endpoint_record_id",
        "manifest_id",
        "position",
        "value",
        "drugbank_approved_percentile",
    }


@pytest.mark.asyncio
async def test_multiple_executions_reuse_one_manifest_and_endpoint_catalog(
    database_session: AsyncSession,
) -> None:
    analysis_id = await create_analysis(database_session)
    output = admet_output()

    await persist_output(database_session, analysis_id, output)
    await persist_output(database_session, analysis_id, output)

    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetModelManifestRecord))
        == 1
    )
    assert await database_session.scalar(select(func.count()).select_from(AdmetEndpointRecord)) == 2
    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetToolExecutionRecord))
        == 2
    )
    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetPredictionRecord)) == 4
    )


@pytest.mark.asyncio
async def test_same_tool_call_is_idempotent_but_changed_content_conflicts(
    database_session: AsyncSession,
) -> None:
    analysis_id = await create_analysis(database_session)
    output = admet_output()
    tool_call_id, request_id, run_id = await persist_output(database_session, analysis_id, output)
    repository = AdmetRepository(database_session)

    _, created = await repository.save(
        tool_call_id=tool_call_id,
        request_id=request_id,
        analysis_id=analysis_id,
        run_id=run_id,
        output=output,
    )
    assert created is False

    changed_result = output.result.model_copy(
        update={
            "predictions": (
                output.result.predictions[0].model_copy(update={"value": 0.99}),
                output.result.predictions[1],
            )
        }
    )
    changed_output = output.model_copy(update={"result": changed_result})
    with pytest.raises(AdmetPersistenceConflict, match="different ADMET content"):
        await repository.save(
            tool_call_id=tool_call_id,
            request_id=request_id,
            analysis_id=analysis_id,
            run_id=run_id,
            output=changed_output,
        )


@pytest.mark.asyncio
async def test_request_id_cannot_create_a_second_tool_call(
    database_session: AsyncSession,
) -> None:
    analysis_id = await create_analysis(database_session)
    output = admet_output()
    _, request_id, _ = await persist_output(database_session, analysis_id, output)

    with pytest.raises(AdmetPersistenceConflict, match="request_id"):
        await AdmetRepository(database_session).save(
            tool_call_id=uuid4(),
            request_id=request_id,
            analysis_id=analysis_id,
            run_id=uuid4(),
            output=output,
        )

    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetToolExecutionRecord))
        == 1
    )


@pytest.mark.asyncio
async def test_same_manifest_hash_with_different_content_is_rejected(
    database_session: AsyncSession,
) -> None:
    analysis_id = await create_analysis(database_session)
    output = admet_output()
    await persist_output(database_session, analysis_id, output)
    changed_manifest = output.manifest.model_copy(update={"tool_version": "different"})
    conflicting_output = output.model_copy(update={"manifest": changed_manifest})

    with pytest.raises(AdmetPersistenceConflict, match="manifest hash"):
        await persist_output(database_session, analysis_id, conflicting_output)


@pytest.mark.asyncio
async def test_analysis_and_output_smiles_must_match_before_any_admet_rows_are_written(
    database_session: AsyncSession,
) -> None:
    analysis_id = await create_analysis(database_session, smiles="CCO")

    with pytest.raises(AdmetPersistenceConflict, match="SMILES"):
        await persist_output(database_session, analysis_id, admet_output(smiles="CCN"))

    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetModelManifestRecord))
        == 0
    )
    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetToolExecutionRecord))
        == 0
    )


@pytest.mark.asyncio
async def test_unknown_analysis_is_rejected_before_any_admet_rows_are_written(
    database_session: AsyncSession,
) -> None:
    with pytest.raises(AdmetPersistenceError, match="analysis does not exist"):
        await persist_output(database_session, uuid4(), admet_output())

    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetModelManifestRecord))
        == 0
    )


@pytest.mark.asyncio
async def test_deleting_analysis_cascades_run_rows_but_preserves_shared_manifest(
    database_session: AsyncSession,
) -> None:
    analysis_id = await create_analysis(database_session)
    await persist_output(database_session, analysis_id, admet_output())

    await database_session.execute(delete(AnalysisRecord).where(AnalysisRecord.id == analysis_id))
    await database_session.commit()

    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetToolExecutionRecord))
        == 0
    )
    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetPredictionRecord)) == 0
    )
    assert (
        await database_session.scalar(select(func.count()).select_from(AdmetModelManifestRecord))
        == 1
    )
    assert await database_session.scalar(select(func.count()).select_from(AdmetEndpointRecord)) == 2


@pytest.mark.asyncio
async def test_prediction_cannot_reference_an_endpoint_from_another_manifest(
    database_session: AsyncSession,
) -> None:
    analysis_id = await create_analysis(database_session)
    first_call, _, _ = await persist_output(database_session, analysis_id, admet_output())
    await persist_output(database_session, analysis_id, admet_output(hash_prefix="e"))
    other_endpoint = await database_session.scalar(
        select(AdmetEndpointRecord)
        .join(AdmetModelManifestRecord)
        .where(AdmetModelManifestRecord.manifest_sha256 == "e" * 64)
        .limit(1)
    )
    first_execution = await database_session.get(AdmetToolExecutionRecord, first_call)
    assert other_endpoint is not None
    assert first_execution is not None

    database_session.add(
        AdmetPredictionRecord(
            tool_call_id=first_call,
            endpoint_record_id=other_endpoint.id,
            manifest_id=first_execution.manifest_id,
            position=99,
            value=0.5,
            drugbank_approved_percentile=50,
        )
    )
    with pytest.raises(IntegrityError):
        await database_session.flush()
    await database_session.rollback()
