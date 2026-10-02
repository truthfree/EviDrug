from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from evidrug_api.analysis_input.models import PotencyEndpoint, TargetMode
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.database import Base
from evidrug_api.decision.dta_lineage import verify_dta_assay_lineage
from evidrug_api.dta.agent import DtaAgentResult, DtaAssayRun, DtaCandidateResult
from evidrug_api.dta.assay_providers import AssayProviderError, AssayProviderErrorCode
from evidrug_api.dta.assay_repository import DtaAssayRepository
from evidrug_api.dta.assay_tables import DtaAssayEvidenceRecord, DtaAssayQueryRecord
from evidrug_api.dta.contracts import DtaModel, DtaObservation, DtaScoreType
from evidrug_api.dta.evidence import (
    AssayEvidence,
    AssayEvidenceKind,
    AssaySource,
    DtaAssayArguments,
    DtaAssayResult,
)
from evidrug_api.orchestration.tables import AgentRunRecord
from evidrug_api.orchestration.upstream import InvalidUpstream
from evidrug_api.tool_admission.bindings import dta_assay_binding
from evidrug_api.tool_admission.contracts import RunContext
from evidrug_api.tool_admission.executor import ToolOutcomeError
from evidrug_api.tool_admission.registry import Invocation

ARGUMENTS = DtaAssayArguments(canonical_smiles="CCO", uniprot_accession="P24941")
EVIDENCE = AssayEvidence(
    source=AssaySource.PUBCHEM,
    source_record_id="aid:1:sid:2:IC50",
    kind=AssayEvidenceKind.QUANTITATIVE,
    endpoint=PotencyEndpoint.IC50,
    value=12.5,
    unit="nM",
    assay_description="cell-free assay",
    doi="10.1000/test",
)


class Provider:
    source = AssaySource.PUBCHEM
    version = "pubchem-test-v1"

    def __init__(self, error: AssayProviderError | None = None) -> None:
        self.error = error
        self.calls = 0

    async def query(self, arguments: DtaAssayArguments) -> DtaAssayResult:
        assert arguments == ARGUMENTS
        self.calls += 1
        if self.error is not None:
            raise self.error
        return DtaAssayResult(
            source=self.source,
            status="succeeded",
            evidence=(EVIDENCE,),
            external_requests=2,
        )


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


async def invocation(session: AsyncSession) -> Invocation:
    analysis = AnalysisRecord(
        session_fingerprint="a" * 64,
        idempotency_key=str(uuid4()),
        disease_id="test",
        disease_name="Test",
        target_mode=TargetMode.SPECIFIED,
        target_name="CDK4",
        original_smiles="CCO",
        canonical_smiles="CCO",
    )
    run_id = uuid4()
    session.add(analysis)
    await session.flush()
    session.add(
        AgentRunRecord(
            run_id=run_id,
            analysis_id=analysis.id,
            agent_name=AnalysisStageName.DTA,
            attempt=1,
            status="running",
            input_sha256="b" * 64,
            started_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return Invocation(
        context=RunContext(
            analysis_id=analysis.id,
            run_id=run_id,
            agent=AnalysisStageName.DTA,
        ),
        request_id=uuid4(),
        tool_call_id=uuid4(),
        timeout_seconds=30,
        session=session,
    )


@pytest.mark.asyncio
async def test_binding_persists_evidence_and_reuses_completed_query(
    session: AsyncSession,
) -> None:
    call = await invocation(session)
    provider = Provider()
    binding = dta_assay_binding(provider)

    first = await binding.invoke(call, ARGUMENTS)
    second = await binding.invoke(call, ARGUMENTS)

    assert first == second
    assert provider.calls == 1
    query = await session.get(DtaAssayQueryRecord, call.tool_call_id)
    assert query is not None
    assert query.provider == "pubchem"
    assert query.provider_version == "pubchem-test-v1"
    assert query.status == "succeeded"
    assert query.external_requests == 2
    assert query.canonical_smiles_sha256 != "CCO"
    assert not hasattr(query, "canonical_smiles")
    assert await session.scalar(select(func.count()).select_from(DtaAssayEvidenceRecord)) == 1


@pytest.mark.asyncio
async def test_decision_lineage_matches_dta_snapshot_to_assay_sql(
    session: AsyncSession,
) -> None:
    call = await invocation(session)
    provider = Provider()
    await dta_assay_binding(provider).invoke(call, ARGUMENTS)
    model_tool_call_id = uuid4()
    candidate = DtaCandidateResult(
        ensembl_id="ENSG00000135446",
        approved_symbol="CDK4",
        uniprot_accession=ARGUMENTS.uniprot_accession,
        target_sequence_sha256="c" * 64,
        tool_call_id=model_tool_call_id,
        status="succeeded",
        model=DtaModel(provider="test", model_id="test", version="1"),
        observations=(
            DtaObservation(
                score_type=DtaScoreType.PREDICTED_PKD,
                value=7.0,
                unit="-log10(Kd [M])",
            ),
        ),
        experimental_evidence=(EVIDENCE,),
        assay_runs=(
            DtaAssayRun(
                tool_id="pubchem_bioassay",
                tool_call_id=call.tool_call_id,
                source=AssaySource.PUBCHEM,
                status="succeeded",
                external_requests=2,
            ),
        ),
    )
    result = DtaAgentResult(source_target_run_id=uuid4(), candidates=(candidate,))

    lineage = await verify_dta_assay_lineage(
        session,
        analysis_id=call.context.analysis_id,
        run_id=call.context.run_id,
        canonical_smiles=ARGUMENTS.canonical_smiles,
        result=result,
    )

    assert len(lineage) == 1
    assert lineage[0].source_tool_call_id == call.tool_call_id

    changed = candidate.model_copy(
        update={"experimental_evidence": (EVIDENCE.model_copy(update={"value": 999.0}),)}
    )
    with pytest.raises(InvalidUpstream, match="decision_dta_assay_evidence_mismatch"):
        await verify_dta_assay_lineage(
            session,
            analysis_id=call.context.analysis_id,
            run_id=call.context.run_id,
            canonical_smiles=ARGUMENTS.canonical_smiles,
            result=result.model_copy(update={"candidates": (changed,)}),
        )


@pytest.mark.asyncio
async def test_binding_persists_failure_and_does_not_repeat_external_request(
    session: AsyncSession,
) -> None:
    call = await invocation(session)
    provider = Provider(AssayProviderError(AssayProviderErrorCode.TIMEOUT))
    binding = dta_assay_binding(provider)

    for _ in range(2):
        with pytest.raises(ToolOutcomeError) as captured:
            await binding.invoke(call, ARGUMENTS)
        assert captured.value.code == "provider_timeout"

    assert provider.calls == 1
    stored = await DtaAssayRepository(session).load(
        tool_call_id=call.tool_call_id,
        request_id=call.request_id,
        analysis_id=call.context.analysis_id,
        run_id=call.context.run_id,
        provider=provider.source,
        provider_version=provider.version,
        arguments=ARGUMENTS,
    )
    assert stored is not None
    assert stored.status == "failed"
    assert stored.result is None
    assert stored.error_code == "provider_timeout"
    assert stored.external_requests == 1


@pytest.mark.asyncio
async def test_repository_preserves_no_records_as_distinct_outcome(
    session: AsyncSession,
) -> None:
    call = await invocation(session)
    now = datetime.now(UTC)
    result = DtaAssayResult(source=AssaySource.BINDINGDB, status="no_records", external_requests=1)
    repository = DtaAssayRepository(session)
    await repository.save(
        tool_call_id=call.tool_call_id,
        request_id=call.request_id,
        analysis_id=call.context.analysis_id,
        run_id=call.context.run_id,
        provider=AssaySource.BINDINGDB,
        provider_version="bindingdb-test-v1",
        arguments=ARGUMENTS,
        status="no_records",
        error_code=None,
        external_requests=1,
        started_at=now,
        finished_at=now,
        duration_ms=0,
        result=result,
    )

    loaded = await repository.load(
        tool_call_id=call.tool_call_id,
        request_id=call.request_id,
        analysis_id=call.context.analysis_id,
        run_id=call.context.run_id,
        provider=AssaySource.BINDINGDB,
        provider_version="bindingdb-test-v1",
        arguments=ARGUMENTS,
    )
    assert loaded is not None
    assert loaded.status == "no_records"
    assert loaded.result == result
