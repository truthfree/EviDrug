"""기존 LLM 결과는 그대로 읽고 두 단계만 새로 실행하는 개발 경계를 검증한다."""

import asyncio
import json
from typing import NoReturn, cast
from uuid import UUID, uuid4

import pytest
from pydantic_settings import SettingsConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_dta_agent import CandidateProvider, FakeReasoner
from test_orchestration import orchestration_database as database_fixture
from test_poc_agents import DecisionClient, FullAdmetProvider
from test_target_hypothesis import create_analysis, target_agent

from evidrug_api.admet.adapter import AdmetToolAdapter
from evidrug_api.admet.agent import AdmetAgent
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.decision.agent import DecisionAgent
from evidrug_api.dta.adapter import DtaToolAdapter
from evidrug_api.dta.agent import DtaAgent
from evidrug_api.execution_contracts.agent import AgentInput
from evidrug_api.openai_gateway.client import DaconOpenAIClient
from evidrug_api.orchestration.continuation import (
    ContinuationExecutor,
    continuation_report,
    output_parsers,
    run_continuation,
)
from evidrug_api.orchestration.contracts import AgentExecutionResult
from evidrug_api.orchestration.executor import RoutedAgentExecutor
from evidrug_api.orchestration.replay_contracts import ReplayError, ReplayManifest, ReplayRequest
from evidrug_api.orchestration.replay_store import ReplayStore
from evidrug_api.orchestration.service import AnalysisOrchestrator
from evidrug_api.orchestration.tables import AgentRunRecord
from evidrug_api.tool_admission.bindings import admet_binding, dta_binding

orchestration_database = database_fixture
TARGET, ADMET, DTA, DECISION = tuple(AnalysisStageName)


async def prepare_case(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[ReplayStore, UUID, ReplayManifest, dict[UUID, str | None]]:
    base = await create_analysis(factory)
    admet = FullAdmetProvider()
    await AnalysisOrchestrator(
        factory,
        RoutedAgentExecutor(
            {
                TARGET: target_agent(),
                ADMET: AdmetAgent(factory, admet_binding(AdmetToolAdapter(admet)), FakeReasoner()),
            }
        ),
    ).run(base)
    async with factory() as session:
        rows = list(
            await session.scalars(select(AgentRunRecord).where(AgentRunRecord.analysis_id == base))
        )
        runs = {row.agent_name: row.run_id for row in rows}
        originals = {row.run_id: row.output_json for row in rows}
    store = ReplayStore(factory, output_parsers())
    request = ReplayRequest(
        base_analysis_id=base,
        source_run_id=runs[DECISION],
        agent_name=DECISION,
        mode="continue_dta_decision",
        upstream_run_ids=(runs[TARGET], runs[ADMET]),
    )
    plan = await store.manifest(request)
    new = await store.prepare(uuid4(), plan)
    assert await store.prepare(new, plan) == new
    return store, new, plan, originals


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fail_dta,decision_mode,expected",
    [
        (False, "normal", "completed"),
        (True, "normal", "failed"),
        (False, "unknown_citation", "failed"),
    ],
)
async def test_continuation_reuses_original_llm_results_and_only_runs_dta_decision(
    orchestration_database: async_sessionmaker[AsyncSession],
    fail_dta: bool,
    decision_mode: str,
    expected: str,
) -> None:
    store, new, plan, originals = await prepare_case(orchestration_database)
    assert (await continuation_report(store, new)).new_runs == ()
    provider = CandidateProvider((1,) if fail_dta else ())
    client = DecisionClient(decision_mode)
    called: list[AnalysisStageName] = []
    routed = RoutedAgentExecutor(
        {
            DTA: DtaAgent(store.factory, dta_binding(DtaToolAdapter(provider)), FakeReasoner()),
            DECISION: DecisionAgent(store.factory, cast(DaconOpenAIClient, client)),
        }
    )

    class Counted:
        async def execute(self, incoming: AgentInput) -> AgentExecutionResult:
            called.append(incoming.agent_name)
            assert incoming.agent_name in {DTA, DECISION}
            return await routed.execute(incoming)

    report = await run_continuation(store, new, Counted(), ())
    assert report.status == expected
    assert len(provider.calls) == 1
    assert len(client.calls) == (0 if fail_dta else 1)
    assert called == ([DTA] if fail_dta else [DTA, DECISION])
    assert report.uncalled_agents == (TARGET, ADMET)
    assert report.reused_runs == plan.upstream
    assert {run.agent_name for run in report.new_runs} == {DTA, DECISION}
    if fail_dta:
        assert report.new_runs[-1].error_code == "skipped_due_to_failed_dta"
    else:
        usage = report.new_runs[-1].new_usage
        assert usage is not None and usage.token_usage is not None
        assert usage.token_usage.total_tokens == 20
    assert await run_continuation(store, new, Counted(), ()) == report
    assert len(provider.calls) == 1
    async with store.factory() as session:
        for run_id, serialized in originals.items():
            original = await session.get(AgentRunRecord, run_id)
            assert original is not None
            assert original.output_json == serialized
        new_rows = list(
            await session.scalars(select(AgentRunRecord).where(AgentRunRecord.analysis_id == new))
        )
        assert len(new_rows) == 2
        dta = next(row for row in new_rows if row.agent_name == DTA)
        assert dta.input_json is not None
        assert (
            AgentInput.model_validate_json(dta.input_json).upstream_outputs[0].run_id
            == plan.upstream[0].run_id
        )
        if not fail_dta:
            assert dta.output_json is not None
            assert json.loads(dta.output_json)["result"]["source_target_run_id"] == str(
                plan.upstream[0].run_id
            )


@pytest.mark.asyncio
async def test_continuation_rejects_wrong_runner_version_and_changed_source_before_calls(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    from evidrug_api.execution_contracts.common import ComponentVersion

    store, new, plan, _ = await prepare_case(orchestration_database)

    class Forbidden:
        async def execute(self, incoming: AgentInput) -> AgentExecutionResult:
            pytest.fail("invalid plans must not call any agent")

    runner = AnalysisOrchestrator(store.factory, Forbidden(), profile=plan.profile)
    with pytest.raises(ReplayError, match="replay_requires_single_agent_runner"):
        await runner.run(new)
    with pytest.raises(ReplayError, match="replay_requires_continuation_runner"):
        await runner.run_single(new, store)
    with pytest.raises(ReplayError, match="replay_runtime_version_mismatch"):
        await run_continuation(
            store, new, Forbidden(), (ComponentVersion(component="x", version="y"),)
        )
    async with store.factory() as session:
        await session.execute(
            update(AgentRunRecord)
            .where(AgentRunRecord.run_id == plan.upstream[0].run_id)
            .values(output_sha256="0" * 64)
        )
        await session.commit()
    with pytest.raises(ReplayError, match="replay_output_hash_mismatch"):
        await run_continuation(store, new, Forbidden(), ())


@pytest.mark.asyncio
async def test_continuation_cannot_route_target_and_rejects_foreign_upstream(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    store, new, plan, _ = await prepare_case(orchestration_database)
    request = plan.request.model_copy(
        update={"upstream_run_ids": (uuid4(), plan.upstream[1].run_id)}
    )
    with pytest.raises(ReplayError, match="replay_run_missing"):
        await store.manifest(request)
    incoming = AgentInput(
        analysis_id=new,
        run_id=uuid4(),
        agent_name=TARGET,
        attempt=1,
        case_input=plan.case_input,
        execution_limits=plan.profile.stage_limits,
    )
    with pytest.raises(ReplayError, match="replay_input_mismatch"):
        await ContinuationExecutor(store, RoutedAgentExecutor({})).execute(incoming)


@pytest.mark.asyncio
async def test_continuation_cancelled_run_is_terminal_and_does_not_retry(
    orchestration_database: async_sessionmaker[AsyncSession],
) -> None:
    store, new, _, _ = await prepare_case(orchestration_database)
    started = asyncio.Event()
    calls: list[AnalysisStageName] = []

    class Waiting:
        async def execute(self, incoming: AgentInput) -> AgentExecutionResult:
            calls.append(incoming.agent_name)
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    task = asyncio.create_task(run_continuation(store, new, Waiting(), ()))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await continuation_report(store, new)).status == "failed"
    await run_continuation(store, new, Waiting(), ())
    assert calls == [DTA]


@pytest.mark.asyncio
async def test_continuation_cli_prepare_and_show_are_free_and_run_requires_live_consent(
    orchestration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from evidrug_api.config import Settings
    from evidrug_api.orchestration import replay_cli

    store, _, plan, _ = await prepare_case(orchestration_database)

    class TestSettings(Settings):
        model_config = SettingsConfigDict(env_file=None)

    settings = TestSettings(
        environment="test",
        poc_models_enabled=True,
        database_url=str(store.factory.kw["bind"].url),
    )

    def forbidden(*args: object) -> NoReturn:
        pytest.fail("prepare/show/unapproved run must not construct live clients")

    monkeypatch.setattr(replay_cli, "build_continuation_executor", forbidden)
    args = replay_cli.parser().parse_args(
        [
            "prepare",
            "--base-analysis-id",
            str(plan.request.base_analysis_id),
            "--source-run-id",
            str(plan.request.source_run_id),
            "--agent",
            "decision",
            "--mode",
            "continue_dta_decision",
            "--upstream-run-id",
            str(plan.upstream[0].run_id),
            "--upstream-run-id",
            str(plan.upstream[1].run_id),
        ]
    )
    assert await replay_cli.execute(args, settings) == 0
    report = json.loads(capsys.readouterr().out)
    new = report["analysis_id"]
    assert report["new_runs"] == []
    assert (
        await replay_cli.execute(
            replay_cli.parser().parse_args(
                [
                    "show",
                    "--analysis-id",
                    new,
                ]
            ),
            settings,
        )
        == 0
    )
    with pytest.raises(ReplayError, match="replay_live_confirmation_required"):
        await replay_cli.execute(
            replay_cli.parser().parse_args(
                [
                    "run",
                    "--analysis-id",
                    new,
                ]
            ),
            settings,
        )
    with pytest.raises(ReplayError, match="replay_poc_worker_required"):
        await replay_cli.execute(
            replay_cli.parser().parse_args(
                [
                    "run",
                    "--analysis-id",
                    new,
                    "--allow-live",
                ]
            ),
            settings.model_copy(update={"poc_models_enabled": False}),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["foreign", "failed", "input_hash", "metadata"])
async def test_reused_admet_must_be_completed_same_case_and_unchanged(
    orchestration_database: async_sessionmaker[AsyncSession],
    mutation: str,
) -> None:
    store, new, plan, _ = await prepare_case(orchestration_database)
    if mutation == "foreign":
        _, _, other, _ = await prepare_case(orchestration_database)
        changed = plan.request.model_copy(
            update={
                "upstream_run_ids": (
                    plan.upstream[0].run_id,
                    other.upstream[1].run_id,
                )
            }
        )
        with pytest.raises(ReplayError, match="replay_origin_mismatch"):
            await store.manifest(changed)
        return
    changes_by_mutation: dict[str, dict[str, object]] = {
        "failed": {"status": "failed", "error_code": "synthetic_failure"},
        "input_hash": {"input_sha256": "0" * 64},
        "metadata": {"execution_metadata_json": None},
    }
    changes = changes_by_mutation[mutation]
    async with store.factory() as session:
        await session.execute(
            update(AgentRunRecord)
            .where(AgentRunRecord.run_id == plan.upstream[1].run_id)
            .values(**changes)
        )
        await session.commit()
    with pytest.raises(ReplayError):
        await store.validate(new)
