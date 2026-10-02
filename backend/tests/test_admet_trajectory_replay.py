from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admet_repository import CountingProvider
from test_tool_execution import database as database_fixture

from evidrug_api.admet.adapter import AdmetToolAdapter
from evidrug_api.admet.contracts import AdmetToolArguments
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.execution_contracts.tool import ToolRequest
from evidrug_api.tool_admission.bindings import admet_binding
from evidrug_api.tool_admission.contracts import RunContext, ToolPin, ToolRunPolicy
from evidrug_api.tool_admission.executor import AdmittedToolExecutor
from evidrug_api.tool_admission.registry import ToolBinding, ToolRegistry
from evidrug_api.trajectory.admet_snapshot import (
    AdmetReplayExecutor,
    AdmetSnapshotError,
    AdmetSnapshotRepository,
)
from evidrug_api.trajectory.tables import AdmetObservationSnapshotRecord

Database = tuple[async_sessionmaker[AsyncSession], UUID]
database = database_fixture


def policy(analysis_id: UUID, binding: ToolBinding) -> ToolRunPolicy:
    return ToolRunPolicy(
        context=RunContext(
            analysis_id=analysis_id,
            run_id=uuid4(),
            agent=AnalysisStageName.ADMET,
        ),
        policy_version="admet-live-test-v1",
        allowed_tools=(ToolPin(tool_id=binding.tool_id, version=binding.version),),
        max_tool_calls=1,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        call_timeout_seconds=5,
    )


def request(
    config: ToolRunPolicy, binding: ToolBinding, *, smiles: str = "CCO"
) -> ToolRequest[AdmetToolArguments]:
    return ToolRequest[AdmetToolArguments](
        request_id=uuid4(),
        run_id=config.context.run_id,
        tool_id=binding.tool_id,
        tool_version=binding.version,
        arguments=AdmetToolArguments(canonical_smiles=smiles),
        objective="독성 관측을 확보한다.",
    )


async def live_call(
    factory: async_sessionmaker[AsyncSession],
    config: ToolRunPolicy,
    binding: ToolBinding,
    payload: ToolRequest[AdmetToolArguments],
    tool_call_id: UUID,
) -> None:
    registry = ToolRegistry((binding,))
    async with factory() as session:
        executor = AdmittedToolExecutor(registry, session)
        await executor.start_run(config)
        await executor.execute(config.context, tool_call_id=tool_call_id, request=payload)


@pytest.mark.asyncio
async def test_snapshot_replays_success_without_calling_provider(database: Database) -> None:
    factory, analysis_id = database
    provider = CountingProvider()
    binding = admet_binding(AdmetToolAdapter(provider))
    config = policy(analysis_id, binding)
    payload, source_call_id = request(config, binding), uuid4()
    await live_call(factory, config, binding, payload, source_call_id)
    assert provider.calls == 1

    async with factory() as session:
        snapshot = await AdmetSnapshotRepository(session).create(
            analysis_id=analysis_id,
            snapshot_version="admet-pilot-v1",
            source_tool_call_ids=(source_call_id,),
        )
        replay_request = payload.model_copy(update={"request_id": uuid4(), "run_id": uuid4()})
        observation = await AdmetReplayExecutor(session).execute(
            snapshot.snapshot_id,
            tool_call_id=uuid4(),
            request=replay_request,
        )

    assert observation.status == "succeeded"
    assert observation.result is not None
    assert observation.raw_result is not None
    assert observation.raw_result.artifact_id == source_call_id
    assert observation.execution_metadata.usage.tool_calls == 0
    assert observation.execution_metadata.usage.external_requests == 0
    assert provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,reason",
    [
        ({"tool_version": "other"}, "replay_tool_version_mismatch"),
        ({"arguments": AdmetToolArguments(canonical_smiles="CCC")}, "replay_input_mismatch"),
        ({"tool_id": "unknown"}, "replay_tool_missing"),
    ],
)
async def test_replay_rejects_unregistered_requests_without_live_fallback(
    database: Database, change: dict[str, object], reason: str
) -> None:
    factory, analysis_id = database
    provider = CountingProvider()
    binding = admet_binding(AdmetToolAdapter(provider))
    config = policy(analysis_id, binding)
    payload, source_call_id = request(config, binding), uuid4()
    await live_call(factory, config, binding, payload, source_call_id)
    async with factory() as session:
        snapshot = await AdmetSnapshotRepository(session).create(
            analysis_id=analysis_id,
            snapshot_version="admet-pilot-v1",
            source_tool_call_ids=(source_call_id,),
        )
        observation = await AdmetReplayExecutor(session).execute(
            snapshot.snapshot_id,
            tool_call_id=uuid4(),
            request=payload.model_copy(update=change),
        )
    assert observation.status == "failed"
    assert observation.error is not None and observation.error.code == reason
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_failed_source_observation_is_replayed_as_failure(database: Database) -> None:
    factory, analysis_id = database
    provider = CountingProvider()
    provider.fail = True
    binding = admet_binding(AdmetToolAdapter(provider))
    config = policy(analysis_id, binding)
    payload, source_call_id = request(config, binding), uuid4()
    await live_call(factory, config, binding, payload, source_call_id)
    async with factory() as session:
        snapshot = await AdmetSnapshotRepository(session).create(
            analysis_id=analysis_id,
            snapshot_version="admet-failure-v1",
            source_tool_call_ids=(source_call_id,),
        )
        observation = await AdmetReplayExecutor(session).execute(
            snapshot.snapshot_id,
            tool_call_id=uuid4(),
            request=payload,
        )
    assert observation.status == "failed"
    assert observation.error is not None
    assert observation.error.code == "provider_unavailable"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_snapshot_detects_manifest_tampering(database: Database) -> None:
    factory, analysis_id = database
    provider = CountingProvider()
    binding = admet_binding(AdmetToolAdapter(provider))
    config = policy(analysis_id, binding)
    payload, source_call_id = request(config, binding), uuid4()
    await live_call(factory, config, binding, payload, source_call_id)
    async with factory() as session:
        repository = AdmetSnapshotRepository(session)
        snapshot = await repository.create(
            analysis_id=analysis_id,
            snapshot_version="admet-pilot-v1",
            source_tool_call_ids=(source_call_id,),
        )
        row = await session.get(AdmetObservationSnapshotRecord, snapshot.snapshot_id)
        assert row is not None
        row.manifest_json = row.manifest_json.replace("admet-pilot-v1", "tampered-v1")
        await session.commit()
        with pytest.raises(AdmetSnapshotError, match="hash mismatch"):
            await repository.load(snapshot.snapshot_id)


@pytest.mark.asyncio
async def test_missing_snapshot_returns_replay_miss_without_provider(database: Database) -> None:
    factory, analysis_id = database
    provider = CountingProvider()
    binding = admet_binding(AdmetToolAdapter(provider))
    config = policy(analysis_id, binding)
    async with factory() as session:
        observation = await AdmetReplayExecutor(session).execute(
            uuid4(),
            tool_call_id=uuid4(),
            request=request(config, binding),
        )
    assert observation.status == "failed"
    assert observation.error is not None
    assert observation.error.code == "replay_snapshot_missing"
    assert provider.calls == 0
