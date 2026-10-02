import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admet_repository import CountingProvider
from test_dta import ARGUMENTS
from test_tool_execution import BlockingProvider
from test_tool_execution import database as database_fixture

from evidrug_api.admet.adapter import AdmetToolAdapter
from evidrug_api.admet.contracts import AdmetToolArguments
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.dta.adapter import DtaProviderUnavailable, DtaToolAdapter
from evidrug_api.dta.contracts import DtaArguments, DtaObservation
from evidrug_api.execution_contracts.tool import ToolObservation, ToolRequest
from evidrug_api.tool_admission.bindings import admet_binding, dta_binding
from evidrug_api.tool_admission.contracts import RunContext, ToolPin, ToolRunPolicy
from evidrug_api.tool_admission.executor import AdmittedToolExecutor
from evidrug_api.tool_admission.registry import ToolBinding, ToolRegistry
from evidrug_api.tool_admission.tables import ToolAdmissionRecord, ToolRunRecord
from evidrug_api.tool_execution.tables import ToolExecutionRecord

Database = tuple[async_sessionmaker[AsyncSession], UUID]
database = database_fixture


def policy(analysis_id: UUID, binding: ToolBinding, **changes: object) -> ToolRunPolicy:
    values: dict[str, object] = dict(
        context=RunContext(analysis_id=analysis_id, run_id=uuid4(), agent=binding.agents[0]),
        policy_version="test-v1",
        allowed_tools=(ToolPin(tool_id=binding.tool_id, version=binding.version),),
        max_tool_calls=1,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        call_timeout_seconds=5,
    )
    values.update(changes)
    return ToolRunPolicy.model_validate(values)


def request(config: ToolRunPolicy, binding: ToolBinding) -> ToolRequest[DtaArguments]:
    return ToolRequest[DtaArguments](
        request_id=uuid4(),
        run_id=config.context.run_id,
        tool_id=binding.tool_id,
        tool_version=binding.version,
        arguments=ARGUMENTS,
        objective="test",
    )


async def start(
    factory: async_sessionmaker[AsyncSession], registry: ToolRegistry, config: ToolRunPolicy
) -> None:
    async with factory() as session:
        await AdmittedToolExecutor(registry, session).start_run(config)


async def dispatch[T: BaseModel](
    factory: async_sessionmaker[AsyncSession],
    registry: ToolRegistry,
    config: ToolRunPolicy,
    payload: ToolRequest[T],
    call_id: UUID,
) -> ToolObservation[BaseModel]:
    async with factory() as session:
        return await AdmittedToolExecutor(registry, session).execute(
            config.context,
            request=payload,
            tool_call_id=call_id,
        )


@pytest.mark.asyncio
async def test_success_replay_and_restart_do_not_reset_budget(database: Database) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    source.release.set()
    binding = dta_binding(DtaToolAdapter(source))
    registry = ToolRegistry((binding,))
    config = policy(analysis_id, binding)
    await start(factory, registry, config)
    payload, call_id = request(config, binding), uuid4()
    first = await dispatch(factory, registry, config, payload, call_id)
    assert first.status == "succeeded" and first.result is not None
    assert first.execution_metadata.usage.tool_calls == 1
    assert first.model_dump(mode="json")["result"]["observations"][0]["value"] == 5.0
    await start(factory, registry, config)
    second = await dispatch(factory, registry, config, payload, call_id)
    assert second.result == first.result and source.calls == 1
    assert second.execution_metadata.usage.tool_calls == 0
    denied = await dispatch(factory, registry, config, request(config, binding), uuid4())
    assert denied.status == "rejected" and denied.error is not None
    assert denied.error.code == "tool_budget_exhausted" and source.calls == 1
    async with factory() as session:
        row = await session.get(ToolRunRecord, config.context.run_id)
        assert row is not None and row.used_calls == 1
        assert await session.scalar(select(func.count()).select_from(ToolAdmissionRecord)) == 2
        assert await session.get(ToolExecutionRecord, call_id) is not None
        with pytest.raises(ValueError, match="immutable"):
            await AdmittedToolExecutor(registry, session).start_run(
                config.model_copy(update={"max_tool_calls": 10})
            )


class LooseArguments(BaseModel):
    canonical_smiles: str = "CCO"
    target_sequence: str = "INVALID123"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,reason",
    [
        ("unknown", "tool_not_allowed"),
        ("version", "tool_version_mismatch"),
        ("arguments", "invalid_arguments"),
        ("run", "run_mismatch"),
        ("disabled", "tool_not_allowed"),
        ("zero", "tool_budget_exhausted"),
        ("expired", "deadline_exceeded"),
    ],
)
async def test_rejected_requests_are_recorded_without_execution(
    database: Database, case: str, reason: str
) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    binding = dta_binding(DtaToolAdapter(source))
    registry = ToolRegistry((binding,))
    changes: dict[str, object] = {}
    if case == "zero":
        changes["max_tool_calls"] = 0
    elif case == "expired":
        changes["deadline"] = datetime.now(UTC) - timedelta(seconds=5)
    elif case == "disabled":
        changes["allowed_tools"] = ()
    config = policy(analysis_id, binding, **changes)
    await start(factory, registry, config)
    payload = request(config, binding)
    update_values: dict[str, object] = {}
    if case == "unknown":
        update_values["tool_id"] = "unknown"
    elif case == "version":
        update_values["tool_version"] = "other"
    elif case == "arguments":
        update_values["arguments"] = LooseArguments()
    elif case == "run":
        update_values["run_id"] = uuid4()
    payload = payload.model_copy(update=update_values)
    result = await dispatch(factory, registry, config, payload, uuid4())
    assert result.status == "rejected" and result.error is not None
    assert result.error.code == reason and source.calls == 0
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ToolExecutionRecord)) == 0
        assert await session.scalar(select(func.count()).select_from(ToolAdmissionRecord)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [AnalysisStageName.TARGET_HYPOTHESIS, AnalysisStageName.DECISION])
async def test_hypothesis_and_decision_have_no_implicit_prediction_permission(
    database: Database, agent: AnalysisStageName
) -> None:
    factory, analysis_id = database
    binding = dta_binding(DtaToolAdapter(BlockingProvider()))
    registry = ToolRegistry((binding,))
    context = RunContext(analysis_id=analysis_id, run_id=uuid4(), agent=agent)
    config = policy(analysis_id, binding, context=context, allowed_tools=())
    await start(factory, registry, config)
    async with factory() as session:
        executor = AdmittedToolExecutor(registry, session)
        assert await executor.schemas(context) == ()
        with pytest.raises(ValueError, match="forbidden"):
            await executor.start_run(
                config.model_copy(
                    update={
                        "allowed_tools": (
                            ToolPin(tool_id=binding.tool_id, version=binding.version),
                        )
                    }
                )
            )


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_overdraw_shared_run_budget(database: Database) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    source.release.set()
    binding = dta_binding(DtaToolAdapter(source))
    registry = ToolRegistry((binding,))
    config = policy(analysis_id, binding)
    await start(factory, registry, config)
    results = await asyncio.gather(
        *(dispatch(factory, registry, config, request(config, binding), uuid4()) for _ in range(4))
    )
    assert sorted(item.status.value for item in results) == ["rejected"] * 3 + ["succeeded"]
    assert source.calls == 1


@pytest.mark.asyncio
async def test_same_request_concurrency_and_conflicting_reuse(database: Database) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    binding = dta_binding(DtaToolAdapter(source))
    registry = ToolRegistry((binding,))
    config = policy(analysis_id, binding)
    await start(factory, registry, config)
    payload, call_id = request(config, binding), uuid4()
    running = asyncio.create_task(dispatch(factory, registry, config, payload, call_id))
    try:
        await asyncio.wait_for(source.started.wait(), 5)
        duplicate = await dispatch(factory, registry, config, payload, call_id)
        assert duplicate.error is not None and duplicate.error.code == "execution_in_progress"
        conflict = await dispatch(
            factory, registry, config, payload.model_copy(update={"objective": "changed"}), call_id
        )
        assert conflict.status == "rejected" and conflict.error is not None
        assert conflict.error.code == "request_conflict"
        assert source.calls == 1
    finally:
        source.release.set()
        await running


@pytest.mark.asyncio
async def test_admet_binding_uses_sql_service_and_omits_manifest(database: Database) -> None:
    factory, analysis_id = database
    source = CountingProvider()
    binding = admet_binding(AdmetToolAdapter(source))
    registry = ToolRegistry((binding,))
    config = policy(analysis_id, binding)
    await start(factory, registry, config)
    payload = ToolRequest[AdmetToolArguments](
        request_id=uuid4(),
        run_id=config.context.run_id,
        tool_id=binding.tool_id,
        tool_version=binding.version,
        arguments=AdmetToolArguments(canonical_smiles="CCO"),
        objective="test",
    )
    call_id = uuid4()
    result = await dispatch(factory, registry, config, payload, call_id)
    replay = await dispatch(factory, registry, config, payload, call_id)
    assert result.status == "succeeded" and replay.result == result.result and source.calls == 1
    assert "manifest" not in result.model_dump(mode="json")["result"]
    async with factory() as session:
        schemas = await AdmittedToolExecutor(registry, session).schemas(config.context)
        assert len(schemas) == 1 and schemas[0]["tool_id"] == "admet_ai"


class UnavailableProvider(BlockingProvider):
    async def predict(self, arguments: DtaArguments) -> tuple[DtaObservation, ...]:
        self.calls += 1
        raise DtaProviderUnavailable("private detail must not escape")


@pytest.mark.asyncio
async def test_unavailable_maps_to_failed_observation_without_fake_result(
    database: Database,
) -> None:
    factory, analysis_id = database
    source = UnavailableProvider()
    binding = dta_binding(DtaToolAdapter(source))
    registry = ToolRegistry((binding,))
    config = policy(analysis_id, binding)
    await start(factory, registry, config)
    payload, call_id = request(config, binding), uuid4()
    for _ in range(2):
        result = await dispatch(factory, registry, config, payload, call_id)
        assert result.status == "failed" and result.result is None and result.error is not None
        assert result.error.code == "provider_unavailable"
        assert "private detail" not in result.model_dump_json()
    assert source.calls == 1


@pytest.mark.asyncio
async def test_call_timeout_is_applied_to_service(database: Database) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    binding = dta_binding(DtaToolAdapter(source))
    registry = ToolRegistry((binding,))
    config = policy(analysis_id, binding, call_timeout_seconds=0.02)
    await start(factory, registry, config)
    result = await dispatch(factory, registry, config, request(config, binding), uuid4())
    assert result.status == "failed" and result.error is not None
    assert result.error.code == "provider_timeout" and source.calls == 1


@pytest.mark.asyncio
async def test_admission_insert_failure_rolls_back_budget_and_never_executes(
    database: Database,
) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    binding = dta_binding(DtaToolAdapter(source))
    registry = ToolRegistry((binding,))
    config = policy(analysis_id, binding)
    await start(factory, registry, config)
    async with factory() as session:
        await session.execute(
            text(
                "CREATE TRIGGER reject_admission BEFORE INSERT ON tool_admissions "
                "BEGIN SELECT RAISE(ABORT, 'test'); END"
            )
        )
        await session.commit()
    with pytest.raises(IntegrityError):
        await dispatch(factory, registry, config, request(config, binding), uuid4())
    async with factory() as session:
        row = await session.get(ToolRunRecord, config.context.run_id)
        assert row is not None and row.used_calls == 0
    assert source.calls == 0


@pytest.mark.asyncio
async def test_trusted_context_must_match_persisted_owner(database: Database) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    binding = dta_binding(DtaToolAdapter(source))
    registry = ToolRegistry((binding,))
    config = policy(analysis_id, binding)
    await start(factory, registry, config)
    wrong_context = config.context.model_copy(update={"agent": AnalysisStageName.ADMET})
    async with factory() as session:
        with pytest.raises(ValueError, match="context mismatch"):
            await AdmittedToolExecutor(registry, session).execute(
                wrong_context,
                request=request(config, binding),
                tool_call_id=uuid4(),
            )
    assert source.calls == 0


@pytest.mark.asyncio
async def test_cancellation_keeps_budget_and_persists_failed_execution(database: Database) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    binding = dta_binding(DtaToolAdapter(source))
    registry = ToolRegistry((binding,))
    config = policy(analysis_id, binding)
    await start(factory, registry, config)
    payload, call_id = request(config, binding), uuid4()
    task = asyncio.create_task(dispatch(factory, registry, config, payload, call_id))
    await asyncio.wait_for(source.started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    replay = await dispatch(factory, registry, config, payload, call_id)
    assert replay.error is not None and replay.error.code == "cancelled"
    assert source.calls == 1
    async with factory() as session:
        row = await session.get(ToolRunRecord, config.context.run_id)
        assert row is not None and row.used_calls == 1


def test_registry_rejects_duplicate_binding_and_unregistered_policy() -> None:
    binding = dta_binding(DtaToolAdapter(BlockingProvider()))
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry((binding, binding))
    with pytest.raises(ValueError, match="unavailable"):
        ToolRegistry(()).validate_policy(policy(uuid4(), binding))


@pytest.mark.asyncio
async def test_simultaneous_identical_admissions_reserve_once(database: Database) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    source.release.set()
    binding = dta_binding(DtaToolAdapter(source))
    registry = ToolRegistry((binding,))
    config = policy(analysis_id, binding)
    await start(factory, registry, config)
    payload, call_id = request(config, binding), uuid4()
    results = await asyncio.gather(
        *(dispatch(factory, registry, config, payload, call_id) for _ in range(3))
    )
    assert sum(item.execution_metadata.usage.tool_calls for item in results) == 1
    assert all(item.admission.decision == "approved" for item in results)
    assert any(item.status == "succeeded" for item in results)
    assert source.calls == 1
    async with factory() as session:
        row = await session.get(ToolRunRecord, config.context.run_id)
        assert row is not None and row.used_calls == 1
        assert await session.scalar(select(func.count()).select_from(ToolAdmissionRecord)) == 1


@pytest.mark.asyncio
async def test_expired_deadline_blocks_previously_approved_replay(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, analysis_id = database
    source = BlockingProvider()
    source.release.set()
    binding = dta_binding(DtaToolAdapter(source))
    registry = ToolRegistry((binding,))
    config = policy(analysis_id, binding)
    await start(factory, registry, config)
    payload, call_id = request(config, binding), uuid4()
    assert (await dispatch(factory, registry, config, payload, call_id)).status == "succeeded"

    async def expired_clock(session: AsyncSession) -> datetime:
        return config.deadline + timedelta(seconds=1)

    monkeypatch.setattr("evidrug_api.tool_admission.executor.database_now", expired_clock)
    result = await dispatch(factory, registry, config, payload, call_id)
    assert result.status == "failed" and result.error is not None
    assert result.error.code == "deadline_exceeded" and source.calls == 1
