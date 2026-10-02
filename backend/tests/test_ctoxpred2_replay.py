from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_ctoxpred2_adapter import provider_report
from test_tool_execution import database as database_fixture

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.ctoxpred2.adapter import CtoxToolAdapter
from evidrug_api.ctoxpred2.contracts import CtoxToolArguments
from evidrug_api.execution_contracts.tool import ToolRequest
from evidrug_api.tool_admission.bindings import ctoxpred2_binding
from evidrug_api.tool_admission.contracts import RunContext, ToolPin, ToolRunPolicy
from evidrug_api.tool_admission.executor import AdmittedToolExecutor
from evidrug_api.tool_admission.registry import ToolBinding, ToolRegistry
from evidrug_api.trajectory.ctox_snapshot import CtoxReplayExecutor, CtoxSnapshotRepository

Database = tuple[async_sessionmaker[AsyncSession], UUID]
database = database_fixture


class Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def predict(self, canonical_smiles: str) -> dict[str, object]:
        self.calls += 1
        report = provider_report()
        report["canonical_smiles"] = canonical_smiles
        return report


def policy(analysis_id: UUID, binding: ToolBinding) -> ToolRunPolicy:
    return ToolRunPolicy(
        context=RunContext(analysis_id=analysis_id, run_id=uuid4(), agent=AnalysisStageName.ADMET),
        policy_version="ctox-live-test-v1",
        allowed_tools=(ToolPin(tool_id=binding.tool_id, version=binding.version),),
        max_tool_calls=1,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        call_timeout_seconds=5,
    )


def request(
    config: ToolRunPolicy, binding: ToolBinding, smiles: str = "CCO"
) -> ToolRequest[CtoxToolArguments]:
    return ToolRequest[CtoxToolArguments](
        request_id=uuid4(),
        run_id=config.context.run_id,
        tool_id=binding.tool_id,
        tool_version=binding.version,
        arguments=CtoxToolArguments(canonical_smiles=smiles),
        objective="심장 이온통로 근거 공백을 보완한다.",
    )


@pytest.mark.asyncio
async def test_ctox_snapshot_replays_without_provider(database: Database) -> None:
    factory, analysis_id = database
    provider = Provider()
    binding = ctoxpred2_binding(CtoxToolAdapter(provider))
    config = policy(analysis_id, binding)
    payload, source_call_id = request(config, binding), uuid4()
    async with factory() as session:
        executor = AdmittedToolExecutor(ToolRegistry((binding,)), session)
        await executor.start_run(config)
        live = await executor.execute(config.context, tool_call_id=source_call_id, request=payload)
        assert live.status == "succeeded"
    assert provider.calls == 1

    async with factory() as session:
        snapshot = await CtoxSnapshotRepository(session).create(
            analysis_id=analysis_id,
            snapshot_version="ctox-breast-cancer-v1",
            source_tool_call_ids=(source_call_id,),
        )
        replay = await CtoxReplayExecutor(session).execute(
            snapshot.snapshot_id,
            tool_call_id=uuid4(),
            request=payload.model_copy(update={"request_id": uuid4(), "run_id": uuid4()}),
            expected_analysis_id=analysis_id,
        )
        other_analysis = await CtoxReplayExecutor(session).execute(
            snapshot.snapshot_id,
            tool_call_id=uuid4(),
            request=payload,
            expected_analysis_id=uuid4(),
        )

    assert replay.status == "succeeded"
    assert replay.raw_result is not None and replay.raw_result.artifact_id == source_call_id
    assert replay.execution_metadata.usage.tool_calls == 0
    assert provider.calls == 1
    assert other_analysis.status == "failed"
    assert other_analysis.error is not None
    assert other_analysis.error.code == "replay_analysis_mismatch"


@pytest.mark.asyncio
async def test_ctox_replay_miss_never_calls_provider(database: Database) -> None:
    factory, analysis_id = database
    provider = Provider()
    binding = ctoxpred2_binding(CtoxToolAdapter(provider))
    config = policy(analysis_id, binding)
    async with factory() as session:
        replay = await CtoxReplayExecutor(session).execute(
            uuid4(),
            tool_call_id=uuid4(),
            request=request(config, binding),
            expected_analysis_id=analysis_id,
        )
    assert replay.status == "failed"
    assert replay.error is not None and replay.error.code == "replay_snapshot_missing"
    assert provider.calls == 0
