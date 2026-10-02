"""실제 저장 경로와 HTTP 조회를 fake provider로 검증한다. 유료 호출은 없다."""

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_analysis_jobs import (
    AnalysisTestContext,
    build_analysis_app,
    request_headers,
)
from test_dta_agent import CandidateProvider, FakeReasoner
from test_poc_agents import DecisionClient, FullAdmetProvider
from test_target_hypothesis import PALBOCICLIB_SMILES, target_agent

from evidrug_api.admet.adapter import AdmetToolAdapter
from evidrug_api.admet.agent import POC_ENDPOINTS, AdmetAgent
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.analysis_jobs.repository import AnalysisRepository
from evidrug_api.database import Base
from evidrug_api.decision.agent import DecisionAgent
from evidrug_api.dta.adapter import DtaToolAdapter
from evidrug_api.dta.agent import DtaAgent
from evidrug_api.evaluation.baseline import extract_baseline_snapshot
from evidrug_api.openai_gateway.client import DaconOpenAIClient
from evidrug_api.orchestration.executor import RoutedAgentExecutor
from evidrug_api.orchestration.service import AnalysisOrchestrator
from evidrug_api.orchestration.tables import AgentReplayRecord, AgentRunRecord
from evidrug_api.tool_admission.bindings import admet_binding, dta_binding


@pytest_asyncio.fixture
async def analysis_context(tmp_path: Path) -> AsyncIterator[AnalysisTestContext]:
    """병렬 Agent는 단일 연결 in-memory 대신 파일 SQLite의 독립 세션을 사용한다."""
    context = await build_analysis_app()
    await context.engine.dispose()
    context.engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'results.sqlite'}")
    async with context.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    context.app.state.database_session_factory = async_sessionmaker(
        context.engine, expire_on_commit=False
    )
    try:
        yield context
    finally:
        await context.engine.dispose()


async def create_case(client: AsyncClient) -> UUID:
    response = await client.post(
        "/api/v1/analyses",
        headers=request_headers(),
        json={
            "disease_id": "MONDO_0007254",
            "disease_name": "breast cancer",
            "target_mode": "specified",
            "target_name": "CDK4",
            "smiles": PALBOCICLIB_SMILES,
        },
    )
    assert response.status_code == 202
    return UUID(response.json()["analysis_id"])


async def execute_case(
    context: AnalysisTestContext, analysis_id: UUID, fail: str = ""
) -> tuple[FullAdmetProvider, CandidateProvider, DecisionClient]:
    factory = async_sessionmaker(context.engine, expire_on_commit=False)
    admet, dta, model = (
        FullAdmetProvider(),
        CandidateProvider((1,) if fail == "dta" else ()),
        DecisionClient(),
    )
    admet.fail = fail == "admet"
    await AnalysisOrchestrator(
        factory,
        RoutedAgentExecutor(
            {
                AnalysisStageName.TARGET_HYPOTHESIS: target_agent(),
                AnalysisStageName.ADMET: AdmetAgent(
                    factory, admet_binding(AdmetToolAdapter(admet)), FakeReasoner()
                ),
                AnalysisStageName.DTA: DtaAgent(
                    factory, dta_binding(DtaToolAdapter(dta)), FakeReasoner()
                ),
                AnalysisStageName.DECISION: DecisionAgent(factory, cast(DaconOpenAIClient, model)),
            }
        ),
    ).run(analysis_id)
    return admet, dta, model


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["", "admet", "dta"])
async def test_baseline_snapshot_preserves_observations_and_failures(
    analysis_context: AnalysisTestContext, failure: str
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=analysis_context.app),
        base_url="http://api.test",
        cookies={"evidrug_session": "owner"},
    ) as client:
        analysis_id = await create_case(client)
        factory = async_sessionmaker(analysis_context.engine, expire_on_commit=False)
        async with factory() as session:
            queued = await extract_baseline_snapshot(AnalysisRepository(session), analysis_id)
            assert queued.dta.projection_status == "unavailable"
            assert queued.admet.projection_status == "unavailable"
        await execute_case(analysis_context, analysis_id, failure)

    async with factory() as session:
        snapshot = await extract_baseline_snapshot(AnalysisRepository(session), analysis_id)
        assert snapshot.analysis_id == analysis_id
        assert snapshot.schema_version == "1"
        assert snapshot.dta.run_id is not None
        assert snapshot.admet.run_id is not None
        for run in (snapshot.dta, snapshot.admet):
            assert run.attempt == 1
            assert run.output_sha256 is None or len(run.output_sha256) == 64
        if failure == "":
            assert snapshot.dta.projection_status == "available"
            assert snapshot.admet.projection_status == "available"
            assert snapshot.dta.result is not None
            assert snapshot.admet.result is not None
            assert snapshot.dta.result.candidates[0].observations
            assert snapshot.admet.result.endpoints
        else:
            failed = snapshot.dta if failure == "dta" else snapshot.admet
            assert failed.projection_status != "available" or failed.status == "partial_failure"


@pytest.mark.asyncio
async def test_baseline_snapshot_rejects_changed_output_hash(
    analysis_context: AnalysisTestContext,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=analysis_context.app),
        base_url="http://api.test",
        cookies={"evidrug_session": "owner"},
    ) as client:
        analysis_id = await create_case(client)
        await execute_case(analysis_context, analysis_id)

    factory = async_sessionmaker(analysis_context.engine, expire_on_commit=False)
    async with factory() as session:
        repository = AnalysisRepository(session)
        run = await repository.get_latest_run(analysis_id, AnalysisStageName.DTA)
        assert run is not None
        run.output_sha256 = "0" * 64
        await session.commit()
        snapshot = await extract_baseline_snapshot(repository, analysis_id)
        assert snapshot.dta.projection_status == "invalid"
        assert snapshot.dta.result is None
        assert snapshot.admet.projection_status == "available"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["", "admet", "dta"])
async def test_results_are_read_only_and_preserve_specialist_states(
    analysis_context: AnalysisTestContext, failure: str
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=analysis_context.app),
        base_url="http://api.test",
        cookies={"evidrug_session": "owner"},
    ) as client:
        analysis_id = await create_case(client)
        url = f"/api/v1/analyses/{analysis_id}/results"
        queued = (await client.get(url)).json()
        assert queued["dta"]["run_id"] is None
        assert queued["dta"]["projection_status"] == "unavailable"
        providers = await execute_case(analysis_context, analysis_id, failure)
        counts = (providers[0].calls, len(providers[1].calls), len(providers[2].calls))
        statements = []

        def record_sql(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            statements.append(statement.lstrip().split()[0].upper())

        event.listen(analysis_context.engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            response = await client.get(url)
            repeated = await client.get(url)
        finally:
            event.remove(analysis_context.engine.sync_engine, "before_cursor_execute", record_sql)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert repeated.json() == response.json()
        assert not {"INSERT", "UPDATE", "DELETE"}.intersection(statements)
        assert counts == (providers[0].calls, len(providers[1].calls), len(providers[2].calls))
        assert len(analysis_context.dispatcher.analysis_ids) == 1
        data = response.json()
        assert [call["agent_name"] for call in data["calls"]] == [
            "target_hypothesis",
            "admet",
            "dta",
            "decision",
        ]
        assert all(call["call_number"] == 1 for call in data["calls"])
        assert all(call["purpose"] == "initial" for call in data["calls"])
        assert data["calls"][1]["result_kind"] == "admet_baseline"
        assert data["calls"][1]["result"] == data["admet"]["result"]
        assert data["decision"]["status"] == "completed"
        assert data["decision"]["result"]["scope"] == "research_prioritization_only"
        assert data["decision"]["result"]["next_actions"][0]["status"] == "proposed"
        assert data["decision"]["result"]["next_actions"][0]["decision_impact"]
        assert data["decision"]["token_usage"]["total_tokens"] == 20
        if failure == "admet":
            assert data["admet"]["status"] == "failed"
            assert data["admet"]["result"] is None
        else:
            summary = data["admet"]["result"]
            assert summary["selected_endpoint_count"] == len(POC_ENDPOINTS)
            assert summary["selection_is_subset"] is True
            assert summary["missing_endpoints"] == []
            assert "value" in summary["endpoints"][0]
        if failure == "dta":
            assert data["dta"]["status"] == "failed"
        else:
            candidate = data["dta"]["result"]["candidates"][0]
            assert candidate["status"] == "succeeded"
            assert candidate["observations"][0]["score_type"] == "predicted_pkd"
            assert candidate["model"]["model_id"] == providers[1].model.model_id
            assert candidate["model_runs"][0]["tool_call_id"] == candidate["tool_call_id"]
            assert candidate["model_runs"][0]["observations"] == candidate["observations"]
        for private in (
            "target_sequence",
            "artifact_sha256",
            "manifest_sha256",
            "session_fingerprint",
            "input_json",
            "output_json",
            "raw_result",
            "canonical_smiles",
        ):
            assert private not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/results"])
async def test_reads_require_owner_and_hide_cli_replays(
    analysis_context: AnalysisTestContext, suffix: str
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=analysis_context.app),
        base_url="http://api.test",
        cookies={"evidrug_session": "owner"},
    ) as client:
        analysis_id = await create_case(client)
        url = f"/api/v1/analyses/{analysis_id}{suffix}"
        assert (await client.get(url)).status_code == 200
        client.cookies.set("evidrug_session", "other")
        denied = await client.get(url)
        missing = await client.get(f"/api/v1/analyses/{uuid4()}{suffix}")
        assert denied.status_code == missing.status_code == 404
        assert denied.json() == missing.json()
        client.cookies.clear()
        assert (await client.get(url)).status_code == 401
        client.cookies.set("evidrug_session", "owner")
        await execute_case(analysis_context, analysis_id)
        factory = async_sessionmaker(analysis_context.engine)
        async with factory() as session:
            run = await session.scalar(
                select(AgentRunRecord).where(AgentRunRecord.analysis_id == analysis_id)
            )
            assert run is not None
            session.add(
                AgentReplayRecord(
                    analysis_id=analysis_id,
                    base_analysis_id=analysis_id,
                    source_run_id=run.run_id,
                    manifest_json="{}",
                    manifest_sha256="0" * 64,
                )
            )
            await session.commit()
        assert (await client.get(url)).status_code == 404
        assert (await client.get("/api/v1/analyses")).json() == {"items": []}


@pytest.mark.asyncio
async def test_results_require_real_authentication() -> None:
    context = await build_analysis_app(authenticated=False)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=context.app), base_url="http://api.test"
        ) as client:
            assert (await client.get(f"/api/v1/analyses/{uuid4()}/results")).status_code == 401
            client.cookies.set("evidrug_session", "invalid-token")
            assert (await client.get(f"/api/v1/analyses/{uuid4()}/results")).status_code == 401
    finally:
        await context.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["json", "schema", "hash", "identity", "partial", "missing", "latest_failed"]
)
async def test_damaged_and_partial_outputs_do_not_hide_other_results(
    analysis_context: AnalysisTestContext, mutation: str
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=analysis_context.app),
        base_url="http://api.test",
        cookies={"evidrug_session": "owner"},
    ) as client:
        analysis_id = await create_case(client)
        await execute_case(analysis_context, analysis_id)
        factory = async_sessionmaker(analysis_context.engine)
        async with factory() as session:
            row = await session.scalar(
                select(AgentRunRecord).where(
                    AgentRunRecord.analysis_id == analysis_id,
                    AgentRunRecord.agent_name == AnalysisStageName.DTA,
                )
            )
            assert row is not None and row.output_json is not None
            payload = json.loads(row.output_json)
            if mutation == "schema":
                payload["schema_version"] = "future"
            elif mutation == "identity":
                payload["run_id"] = str(uuid4())
            elif mutation == "partial":
                row.status = payload["status"] = "partial_failure"
                row.error_code = "reasoning_invalid_output"
                payload["result"]["interpretation"] = None
                payload["error"] = {
                    "code": row.error_code,
                    "message": "private message",
                    "retryable": False,
                }
                payload["warnings"] = [
                    {"code": "reasoning_schema_invalid", "message": "private warning"}
                ]
            if mutation in {"missing", "latest_failed"}:
                if mutation == "latest_failed":
                    row = AgentRunRecord(
                        analysis_id=analysis_id,
                        agent_name=AnalysisStageName.DTA,
                        attempt=2,
                        input_sha256="0" * 64,
                        started_at=datetime.now(UTC),
                        finished_at=datetime.now(UTC),
                    )
                    session.add(row)
                row.status, row.error_code, row.output_json = "failed", "agent_not_configured", None
            else:
                row.output_json = (
                    "sensitive invalid JSON" if mutation == "json" else json.dumps(payload)
                )
                row.output_sha256 = (
                    "0" * 64
                    if mutation == "hash"
                    else hashlib.sha256(row.output_json.encode()).hexdigest()
                )
            await session.commit()
        response = await client.get(f"/api/v1/analyses/{analysis_id}/results")
        assert response.status_code == 200
        data = response.json()
        assert data["admet"]["projection_status"] == "available"
        if mutation == "partial":
            assert data["dta"]["status"] == "partial_failure"
            assert data["dta"]["result"]["interpretation"] is None
            assert data["dta"]["result"]["candidates"][0]["status"] == "succeeded"
            assert data["dta"]["warning_codes"] == ["reasoning_schema_invalid"]
        else:
            assert data["dta"]["result"] is None
            expected = "unavailable" if mutation in {"missing", "latest_failed"} else "invalid"
            assert data["dta"]["projection_status"] == expected
        assert "private message" not in response.text
        assert "private warning" not in response.text
        assert "sensitive invalid JSON" not in response.text


@pytest.mark.asyncio
async def test_specialist_result_schema_is_in_openapi(
    analysis_context: AnalysisTestContext,
) -> None:
    from evidrug_api.analysis_jobs.result_models import SpecialistResultsResponse

    schema = SpecialistResultsResponse.model_json_schema()
    openapi = analysis_context.app.openapi()
    route = openapi["paths"]["/api/v1/analyses/{analysis_id}/results"]["get"]
    assert route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/SpecialistResultsResponse"
    )
    assert set(schema["properties"]) == {
        "analysis_id",
        "status",
        "admet",
        "dta",
        "decision",
        "calls",
    }
    for private in ("target_sequence", "artifact_sha256", "manifest_sha256", "raw_result"):
        assert private not in json.dumps(schema)
