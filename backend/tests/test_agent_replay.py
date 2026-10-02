import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_orchestration import ScriptedAgentExecutor, successful_outcomes
from test_target_hypothesis import PALBOCICLIB_CANONICAL_SMILES, PALBOCICLIB_SMILES

from evidrug_api.analysis_input.models import TargetMode
from evidrug_api.analysis_jobs.models import AnalysisStageName, AnalysisStatus
from evidrug_api.analysis_jobs.results import project_dta
from evidrug_api.analysis_jobs.tables import AnalysisRecord, AnalysisStageRecord
from evidrug_api.database import Base
from evidrug_api.dta.agent import DtaAgentResult
from evidrug_api.execution_contracts.agent import (
    AgentInput,
    AgentOutput,
    EvidenceClaim,
    EvidenceDirection,
)
from evidrug_api.execution_contracts.common import ComponentVersion, ExecutionUsage, TokenUsage
from evidrug_api.orchestration.contracts import AgentExecutionResult
from evidrug_api.orchestration.replay import replay_report, run_replay
from evidrug_api.orchestration.replay_contracts import ReplayError, ReplayRequest
from evidrug_api.orchestration.replay_store import ReplayStore, digest
from evidrug_api.orchestration.repository import (
    OrchestrationInProgress,
    OrchestrationLeaseLost,
    OrchestrationRepository,
)
from evidrug_api.orchestration.service import AnalysisOrchestrator
from evidrug_api.orchestration.tables import AgentRunRecord, AnalysisExecutionRecord

TARGET = AnalysisStageName.TARGET_HYPOTHESIS


class ReplayTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str


def parse_output(serialized: str) -> AgentOutput[BaseModel]:
    return cast(
        AgentOutput[BaseModel], AgentOutput[ReplayTestResult].model_validate_json(serialized)
    )


class CountedExecutor(ScriptedAgentExecutor):
    """실제 외부 호출 없이 stage별 호출 수와 비용을 관찰한다."""

    def __init__(self) -> None:
        super().__init__(successful_outcomes())
        self.inputs: list[AgentInput] = []

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        self.inputs.append(agent_input)
        result = await super().execute(agent_input)
        usage = ExecutionUsage(
            token_usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            external_requests=1,
        )
        metadata = result.output.execution_metadata.model_copy(
            update={
                "usage": usage,
                "components": (ComponentVersion(component="test_model", version="fake-v1"),),
            }
        )
        claim = EvidenceClaim(
            claim_id=uuid4(),
            claim_type="test_only",
            statement="breast cancer POC test evidence",
            direction=EvidenceDirection.UNCERTAIN,
            source="fake provider",
            retrieved_at=datetime.now(UTC),
            producer_run_id=agent_input.run_id,
        )
        return AgentExecutionResult(
            output=result.output.model_copy(
                update={"execution_metadata": metadata, "evidence_claims": (claim,)}
            ),
            provides_dta_input=result.provides_dta_input,
        )


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ReplayStore]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'replay.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield ReplayStore(factory, {name: parse_output for name in AnalysisStageName})
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def source(store: ReplayStore) -> tuple[UUID, dict[AnalysisStageName, UUID]]:
    async with store.factory() as session:
        analysis = AnalysisRecord(
            session_fingerprint="a" * 64,
            idempotency_key=str(uuid4()),
            disease_id="MONDO_0007254",
            disease_name="breast cancer",
            target_mode=TargetMode.SPECIFIED,
            target_name="CDK4",
            original_smiles=PALBOCICLIB_SMILES,
            canonical_smiles=PALBOCICLIB_CANONICAL_SMILES,
            stages=[
                AnalysisStageRecord(name=name, position=index)
                for index, name in enumerate(AnalysisStageName)
            ],
        )
        session.add(analysis)
        await session.commit()
        analysis_id = analysis.id
    await AnalysisOrchestrator(store.factory, CountedExecutor()).run(analysis_id)
    async with store.factory() as session:
        runs = list(await session.scalars(select(AgentRunRecord)))
    return analysis_id, {AnalysisStageName(run.agent_name): run.run_id for run in runs}


def request_for(
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
    agent: AnalysisStageName = TARGET,
    *,
    reuse: bool = False,
) -> ReplayRequest:
    analysis_id, runs = source
    upstream = {
        TARGET: (),
        AnalysisStageName.ADMET: (),
        AnalysisStageName.DTA: (runs[TARGET],),
        AnalysisStageName.DECISION: (
            runs[TARGET],
            runs[AnalysisStageName.ADMET],
            runs[AnalysisStageName.DTA],
        ),
    }
    return ReplayRequest(
        base_analysis_id=analysis_id,
        source_run_id=runs[agent],
        agent_name=agent,
        mode="reuse_output" if reuse else "live_agent",
        upstream_run_ids=upstream[agent],
    )


async def prepare(store: ReplayStore, request: ReplayRequest) -> UUID:
    return await store.prepare(uuid4(), await store.manifest(request))


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", list(AnalysisStageName))
async def test_only_selected_agent_runs_and_original_rows_are_unchanged(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
    agent: AnalysisStageName,
) -> None:
    async with store.factory() as session:
        before = list((await session.execute(select(AgentRunRecord.__table__))).all())
    request = request_for(source, agent)
    analysis_id = await prepare(store, request)
    executor = CountedExecutor()

    report = await run_replay(store, analysis_id, live_executor=executor)

    assert report.status is AnalysisStatus.COMPLETED
    assert executor.calls == [agent]
    assert {item.run_id for item in executor.inputs[0].upstream_outputs} == set(
        request.upstream_run_ids
    )
    assert report.new_usage is not None and report.new_usage.token_usage is not None
    assert report.new_usage.token_usage.total_tokens == 15
    assert report.original_source_usage is not None
    assert report.original_source_usage.token_usage is not None
    assert report.original_source_usage.token_usage.total_tokens == 15
    assert {run.run_id for run in report.reused_runs} == set(request.upstream_run_ids)
    async with store.factory() as session:
        after = list(
            (
                await session.execute(
                    select(AgentRunRecord.__table__).where(AgentRunRecord.analysis_id == source[0])
                )
            ).all()
        )
        assert before == after
        new_run = await session.get(AgentRunRecord, report.new_run_id)
        assert new_run is not None and new_run.parent_run_id == request.source_run_id
        assert new_run.input_json is not None
        assert digest(new_run.input_json) == new_run.input_sha256
    # 동일한 단독 분석 ID는 다시 호출해도 과금되지 않는다.
    assert await run_replay(store, analysis_id, live_executor=executor) == report
    assert executor.calls == [agent]


@pytest.mark.asyncio
async def test_output_reuse_makes_zero_calls_and_preserves_original_cost_and_claims(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    analysis_id = await prepare(store, request_for(source, reuse=True))
    executor = CountedExecutor()
    report = await run_replay(store, analysis_id, live_executor=executor)
    assert executor.calls == []
    assert report.new_usage == ExecutionUsage(
        token_usage=TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0)
    )
    assert (
        report.original_source_usage is not None
        and report.original_source_usage.external_requests == 1
    )
    assert set(report.uncalled_agents) == set(AnalysisStageName)
    async with store.factory() as session:
        original = await session.get(AgentRunRecord, source[1][TARGET])
        copied = await session.get(AgentRunRecord, report.new_run_id)
        assert original is not None and copied is not None
        original_output = store.parse_output(original)
        copied_output = store.parse_output(copied)
        assert original_output.result == copied_output.result
        assert copied_output.evidence_claims[0].producer_run_id == report.new_run_id
        assert original_output.evidence_claims[0].producer_run_id == source[1][TARGET]
        assert copied_output.warnings[-1].code == "stored_output_reused"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["legacy", "pubchem-recall-v1"])
async def test_dta_snapshot_reuse_preserves_policy_without_live_calls(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
    version: str,
) -> None:
    stage = AnalysisStageName.DTA
    async with store.factory() as session:
        row = await session.get(AgentRunRecord, source[1][stage])
        assert row is not None and row.output_json is not None
        document = json.loads(row.output_json)
        document["result"] = {
            "source_target_run_id": str(source[1][TARGET]),
            "candidates": [
                {
                    "ensembl_id": "ENSG00000135446",
                    "approved_symbol": "CDK4",
                    "uniprot_accession": "P11802",
                    "target_sequence_sha256": "a" * 64,
                    "status": "succeeded",
                    "tool_call_id": str(uuid4()),
                    "model": {"provider": "test", "model_id": "cnn", "version": "1"},
                    "observations": [
                        {"score_type": "predicted_pkd", "value": 7.5, "unit": "-log10(Kd [M])"}
                    ],
                }
            ],
        }
        if version != "legacy":
            document["result"]["assay_policy_version"] = version
        row.output_json = json.dumps(document)
        row.output_sha256 = digest(row.output_json)
        await session.commit()

    def parse_dta(serialized: str) -> AgentOutput[BaseModel]:
        return cast(
            AgentOutput[BaseModel], AgentOutput[DtaAgentResult].model_validate_json(serialized)
        )

    store.parsers = {**store.parsers, stage: parse_dta}
    analysis_id = await prepare(store, request_for(source, stage, reuse=True))
    executor = CountedExecutor()
    report = await run_replay(store, analysis_id, live_executor=executor)
    assert report.status is AnalysisStatus.COMPLETED and executor.calls == []
    assert report.new_usage is not None and report.new_usage.external_requests == 0
    async with store.factory() as session:
        copied = await session.get(AgentRunRecord, report.new_run_id)
        assert copied is not None and copied.output_json is not None
        output = AgentOutput[DtaAgentResult].model_validate_json(copied.output_json)
        assert output.result is not None and output.result.assay_policy_version == version
        assert project_dta(output.result).assay_policy_version == version
        components = {item.component: item.version for item in output.execution_metadata.components}
        assert components["dta_assay_policy"] == version


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "failed", "partial_failure", "skipped"])
async def test_incomplete_upstream_is_rejected_before_any_execution(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
    status: str,
) -> None:
    async with store.factory() as session:
        changes: dict[str, object] = {"status": status}
        if status == "running":
            changes.update(finished_at=None, error_code=None)
        elif status in {"failed", "skipped"}:
            changes.update(error_code="test_failure")
        await session.execute(
            update(AgentRunRecord)
            .where(AgentRunRecord.run_id == source[1][TARGET])
            .values(**changes)
        )
        await session.commit()
    with pytest.raises(ReplayError, match="replay_output_not_completed"):
        await prepare(store, request_for(source, AnalysisStageName.DTA))


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["schema", "payload", "input", "identity", "metadata"])
async def test_invalid_saved_output_or_input_is_rejected(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
    tamper: str,
) -> None:
    async with store.factory() as session:
        run = await session.get(AgentRunRecord, source[1][TARGET])
        assert run is not None and run.output_json is not None and run.input_json is not None
        data = json.loads(run.output_json)
        if tamper == "schema":
            data["schema_version"] = "999"
        elif tamper == "payload":
            data["result"] = {"unexpected": "shape"}
        elif tamper == "identity":
            data["analysis_id"] = str(uuid4())
        elif tamper == "metadata":
            data["execution_metadata"]["implementation_version"] = "forged-version"
        else:
            input_data = json.loads(run.input_json)
            input_data["case_input"]["target_name"] = "ESR1"
            run.input_json = json.dumps(input_data)
            run.input_sha256 = digest(run.input_json)
        run.output_json = json.dumps(data)
        run.output_sha256 = digest(run.output_json)
        await session.commit()
    with pytest.raises(
        ReplayError,
        match="replay_(schema_mismatch|input_mismatch|output_identity_mismatch|metadata_mismatch)",
    ):
        await prepare(store, request_for(source, reuse=True))


@pytest.mark.asyncio
async def test_different_analysis_and_missing_snapshot_are_not_live_fallbacks(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    copied_id = await prepare(store, request_for(source, reuse=True))
    await run_replay(store, copied_id)
    request = request_for(source).model_copy(update={"base_analysis_id": copied_id})
    with pytest.raises(ReplayError, match="replay_origin_mismatch"):
        await store.manifest(request)
    request = request_for(source).model_copy(update={"mode": "target_from_snapshot"})
    with pytest.raises(ReplayError, match="replay_evidence_snapshot_missing"):
        await store.manifest(request)


@pytest.mark.asyncio
async def test_legacy_independent_input_can_be_reconstructed_only_with_matching_hash(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    async with store.factory() as session:
        await session.execute(update(AgentRunRecord).values(input_json=None))
        await session.commit()
    # 실제 배포된 과거 Target run은 upstream 없이 입력 hash를 정확히 복원할 수 있다.
    analysis_id = await prepare(store, request_for(source, reuse=True))
    assert (await run_replay(store, analysis_id)).status is AnalysisStatus.COMPLETED
    with pytest.raises(ReplayError, match="replay_input_snapshot_missing"):
        await store.manifest(request_for(source, AnalysisStageName.DTA))
    async with store.factory() as session:
        await session.execute(
            update(AgentRunRecord)
            .where(AgentRunRecord.run_id == source[1][TARGET])
            .values(input_sha256="0" * 64)
        )
        await session.commit()
    with pytest.raises(ReplayError, match="replay_input_hash_mismatch"):
        await store.manifest(request_for(source, reuse=True))


@pytest.mark.asyncio
async def test_plan_is_idempotent_but_changed_mode_or_runtime_is_rejected(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    manifest = await store.manifest(request_for(source))
    analysis_id = uuid4()
    assert await store.prepare(analysis_id, manifest) == analysis_id
    assert await store.prepare(analysis_id, manifest) == analysis_id
    other = await store.manifest(request_for(source, reuse=True))
    with pytest.raises(ReplayError, match="replay_analysis_id_conflict"):
        await store.prepare(analysis_id, other)
    executor = CountedExecutor()
    with pytest.raises(ReplayError, match="replay_runtime_version_mismatch"):
        await run_replay(
            store,
            analysis_id,
            live_executor=executor,
            runtime_versions=(ComponentVersion(component="model", version="changed"),),
        )
    assert executor.calls == []
    with pytest.raises(ReplayError, match="replay_requires_single_agent_runner"):
        await AnalysisOrchestrator(store.factory, executor).run(analysis_id)
    assert executor.calls == []


@pytest.mark.asyncio
async def test_concurrent_delivery_calls_agent_once(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    analysis_id = await prepare(store, request_for(source))
    executor = CountedExecutor()
    executor.block(TARGET)
    task = asyncio.create_task(run_replay(store, analysis_id, live_executor=executor))
    try:
        await asyncio.wait_for(executor.started[TARGET].wait(), 3)
        with pytest.raises(OrchestrationInProgress):
            await run_replay(store, analysis_id, live_executor=executor)
    finally:
        executor.release[TARGET].set()
    assert (await task).status is AnalysisStatus.COMPLETED
    assert executor.calls == [TARGET]


@pytest.mark.asyncio
async def test_cancellation_is_terminal_and_is_not_retried(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    analysis_id = await prepare(store, request_for(source))
    executor = CountedExecutor()
    executor.block(TARGET)
    task = asyncio.create_task(run_replay(store, analysis_id, live_executor=executor))
    await asyncio.wait_for(executor.started[TARGET].wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = await replay_report(store, analysis_id)
    assert report.status is AnalysisStatus.FAILED and report.error_code == "cancelled"
    assert report.new_usage is None  # 불명확한 원격 비용을 0으로 단정하지 않는다.
    await run_replay(store, analysis_id, live_executor=executor)
    assert executor.calls == [TARGET]


@pytest.mark.asyncio
async def test_expired_lease_rejects_late_results(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    analysis_id = await prepare(store, request_for(source))
    executor = CountedExecutor()
    executor.block(TARGET)
    task = asyncio.create_task(run_replay(store, analysis_id, live_executor=executor))
    try:
        await asyncio.wait_for(executor.started[TARGET].wait(), 3)
        async with store.factory() as session:
            await session.execute(
                update(AnalysisExecutionRecord)
                .where(AnalysisExecutionRecord.analysis_id == analysis_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
            await OrchestrationRepository(session).recover_expired()
    finally:
        executor.release[TARGET].set()
    with pytest.raises(OrchestrationLeaseLost):
        await task
    report = await replay_report(store, analysis_id)
    assert report.error_code == "lease_expired" and report.new_usage is None


@pytest.mark.asyncio
async def test_source_changed_after_prepare_is_rejected_without_call(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    analysis_id = await prepare(store, request_for(source))
    async with store.factory() as session:
        run = await session.get(AgentRunRecord, source[1][TARGET])
        assert run is not None and run.output_json is not None
        data = json.loads(run.output_json)
        data["result"]["summary"] = "changed after prepare"
        run.output_json = json.dumps(data)
        run.output_sha256 = digest(run.output_json)
        await session.commit()
    executor = CountedExecutor()
    with pytest.raises(ReplayError, match="replay_source_changed"):
        await run_replay(store, analysis_id, live_executor=executor)
    assert executor.calls == []


@pytest.mark.asyncio
async def test_unknown_output_schema_is_not_accepted_as_generic_json(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    unregistered = ReplayStore(store.factory, {})
    with pytest.raises(ReplayError, match="replay_schema_not_registered"):
        await unregistered.manifest(request_for(source, reuse=True))


@pytest.mark.asyncio
async def test_missing_upstream_and_broken_payload_hash_do_not_create_plan(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    request = request_for(source, AnalysisStageName.DTA).model_copy(update={"upstream_run_ids": ()})
    with pytest.raises(ReplayError, match="replay_upstream_set_mismatch"):
        await prepare(store, request)
    async with store.factory() as session:
        await session.execute(
            update(AgentRunRecord)
            .where(AgentRunRecord.run_id == source[1][TARGET])
            .values(output_sha256="0" * 64)
        )
        await session.commit()
    with pytest.raises(ReplayError, match="replay_output_hash_mismatch"):
        await prepare(store, request_for(source, reuse=True))
    async with store.factory() as session:
        assert list(await session.scalars(select(AnalysisRecord.id))) == [source[0]]


@pytest.mark.asyncio
async def test_changed_dta_lineage_is_rejected(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    async with store.factory() as session:
        run = await session.get(AgentRunRecord, source[1][AnalysisStageName.DTA])
        assert run is not None and run.input_json is not None
        data = json.loads(run.input_json)
        data["upstream_outputs"][0]["output"]["sha256"] = "0" * 64
        run.input_json = json.dumps(data)
        run.input_sha256 = digest(run.input_json)
        await session.commit()
    with pytest.raises(ReplayError, match="replay_upstream_lineage_mismatch"):
        await prepare(store, request_for(source, AnalysisStageName.DECISION))


@pytest.mark.asyncio
async def test_source_failure_can_be_retried_but_cannot_be_reused(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    async with store.factory() as session:
        await session.execute(
            update(AgentRunRecord)
            .where(AgentRunRecord.run_id == source[1][TARGET])
            .values(
                status="failed",
                error_code="agent_not_configured",
                output_json=None,
                output_sha256=None,
                execution_metadata_json=None,
            )
        )
        await session.commit()
    with pytest.raises(ReplayError, match="replay_output_not_completed"):
        await prepare(store, request_for(source, reuse=True))
    analysis_id = await prepare(store, request_for(source))
    executor = CountedExecutor()
    assert (
        await run_replay(store, analysis_id, live_executor=executor)
    ).status is AnalysisStatus.COMPLETED
    assert executor.calls == [TARGET]


@pytest.mark.asyncio
async def test_simultaneous_prepare_with_same_id_creates_one_plan(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    manifest = await store.manifest(request_for(source, reuse=True))
    analysis_id = uuid4()
    results = await asyncio.gather(
        store.prepare(analysis_id, manifest), store.prepare(analysis_id, manifest)
    )
    assert len(results) == 2 and all(result == analysis_id for result in results)
    assert (await run_replay(store, analysis_id)).status is AnalysisStatus.COMPLETED


@pytest.mark.asyncio
async def test_timeout_does_not_fallback_or_claim_zero_usage(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
) -> None:
    class TimeoutExecutor:
        async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
            raise TimeoutError

    analysis_id = await prepare(store, request_for(source))
    report = await run_replay(store, analysis_id, live_executor=TimeoutExecutor())
    assert report.status is AnalysisStatus.FAILED
    assert report.error_code == "agent_timeout"
    assert report.new_usage is None


@pytest.mark.asyncio
async def test_cli_requires_explicit_paid_execution_and_reuses_without_client(
    store: ReplayStore,
    source: tuple[UUID, dict[AnalysisStageName, UUID]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from evidrug_api.config import Settings
    from evidrug_api.orchestration import replay_cli

    # CLI 경계 검증에서도 과학 결과는 fake schema로만 취급한다.
    monkeypatch.setattr(replay_cli, "parse_target_output", parse_output)

    def forbidden_client(settings: Settings) -> None:
        pytest.fail("무료/미승인 경로에서 외부 client를 구성하면 안 된다")

    monkeypatch.setattr(replay_cli, "build_target_hypothesis_agent", forbidden_client)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="test",
        database_url=str(store.factory.kw["bind"].url),
    )
    args = replay_cli.parser().parse_args(
        [
            "prepare",
            "--base-analysis-id",
            str(source[0]),
            "--source-run-id",
            str(source[1][TARGET]),
            "--agent",
            "target_hypothesis",
            "--mode",
            "reuse_output",
        ]
    )
    assert await replay_cli.execute(args, settings) == 0
    analysis_id = json.loads(capsys.readouterr().out)["analysis_id"]
    run_args = replay_cli.parser().parse_args(["run", "--analysis-id", analysis_id])
    assert await replay_cli.execute(run_args, settings) == 0
    assert json.loads(capsys.readouterr().out)["new_usage"]["token_usage"]["total_tokens"] == 0
    args.mode = "live_agent"
    assert await replay_cli.execute(args, settings) == 0
    run_args.analysis_id = UUID(json.loads(capsys.readouterr().out)["analysis_id"])
    with pytest.raises(ReplayError, match="replay_live_confirmation_required"):
        await replay_cli.execute(run_args, settings)
    with pytest.raises(ReplayError, match="replay_development_only"):
        await replay_cli.execute(
            run_args, settings.model_copy(update={"environment": "production"})
        )
