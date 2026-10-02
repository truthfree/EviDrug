from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_admet_repository import admet_output, create_analysis
from test_admet_repository import database_session as database_fixture

from evidrug_api.admet.context import (
    AdmetContext,
    AdmetContextBuilder,
    AdmetContextQuery,
    AdmetContextUnavailable,
)
from evidrug_api.admet.repository import AdmetRepository
from evidrug_api.admet.tables import (
    AdmetEndpointRecord,
    AdmetManifestLimitationRecord,
    AdmetModelManifestRecord,
    AdmetPredictionRecord,
)

database_session = database_fixture


async def seed(session: AsyncSession) -> tuple[UUID, UUID]:
    analysis_id, call_id = await create_analysis(session), uuid4()
    await AdmetRepository(session).save(
        tool_call_id=call_id,
        analysis_id=analysis_id,
        request_id=uuid4(),
        run_id=uuid4(),
        output=admet_output(),
    )
    return analysis_id, call_id


@pytest.mark.asyncio
async def test_selected_rows_preserve_values_units_order_and_provenance(
    database_session: AsyncSession,
) -> None:
    analysis_id, call_id = await seed(database_session)
    query = AdmetContextQuery(source_tool_call_id=call_id, endpoint_ids=("Caco2_Wang", "AMES"))
    builder = AdmetContextBuilder(database_session)
    result = await builder.build(analysis_id=analysis_id, query=query)
    assert [row.endpoint_id for row in result.rows] == ["Caco2_Wang", "AMES"]
    assert result.rows[0].value == -4.71 and result.rows[0].units == "log cm/s"
    assert result.rows[1].value == 0.21 and result.rows[1].drugbank_approved_percentile == 32.5
    assert result.rows[1].units is None
    assert result.rows[1].source_url == admet_output().manifest.endpoints[0].source_url
    assert result.reference_sha256 == "c" * 64 and result.manifest_sha256 == "a" * 64
    assert result.limitations == admet_output().manifest.limitations
    assert not result.is_partial and result.catalog_endpoint_count == 2
    assert result == await builder.build(analysis_id=analysis_id, query=query)
    assert AdmetContext.model_validate_json(result.model_dump_json()) == result
    assert isinstance(result.model_dump(mode="json")["rows"][0], list)


@pytest.mark.asyncio
async def test_partial_context_omits_unused_metadata_and_is_smaller(
    database_session: AsyncSession,
) -> None:
    analysis_id, call_id = await seed(database_session)
    result = await AdmetContextBuilder(database_session).build(
        analysis_id=analysis_id,
        query=AdmetContextQuery(source_tool_call_id=call_id, endpoint_ids=("AMES",)),
    )
    compact = result.model_dump_json()
    assert result.is_partial and len(result.rows) == 1
    for unused in ("Caco2_Wang", "dataset_size", "auroc", "model_artifacts", "canonical_smiles"):
        assert unused not in compact
    assert len(compact.encode()) < len(admet_output().model_dump_json().encode())


@pytest.mark.asyncio
async def test_missing_and_cross_analysis_sources_are_indistinguishable(
    database_session: AsyncSession,
) -> None:
    analysis_id, call_id = await seed(database_session)
    other_analysis = await create_analysis(database_session)
    for owner, source in ((analysis_id, uuid4()), (other_analysis, call_id)):
        with pytest.raises(AdmetContextUnavailable) as error:
            await AdmetContextBuilder(database_session).build(
                analysis_id=owner,
                query=AdmetContextQuery(source_tool_call_id=source, endpoint_ids=("AMES",)),
            )
        assert error.value.code == "result_not_found"


@pytest.mark.asyncio
async def test_unknown_or_missing_prediction_is_not_silently_dropped(
    database_session: AsyncSession,
) -> None:
    analysis_id, call_id = await seed(database_session)
    builder = AdmetContextBuilder(database_session)
    with pytest.raises(AdmetContextUnavailable, match="endpoint_unavailable"):
        await builder.build(
            analysis_id=analysis_id,
            query=AdmetContextQuery(
                source_tool_call_id=call_id,
                endpoint_ids=("AMES", "unknown"),
            ),
        )
    await database_session.execute(delete(AdmetPredictionRecord))
    await database_session.commit()
    with pytest.raises(AdmetContextUnavailable, match="endpoint_unavailable"):
        await builder.build(
            analysis_id=analysis_id,
            query=AdmetContextQuery(
                source_tool_call_id=call_id,
                endpoint_ids=("AMES",),
            ),
        )


@pytest.mark.parametrize(
    "endpoints", [(), ("AMES", "AMES"), ("",), (" ",), tuple(str(i) for i in range(21))]
)
def test_invalid_selection_is_rejected(endpoints: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        AdmetContextQuery(source_tool_call_id=uuid4(), endpoint_ids=endpoints)


@pytest.mark.asyncio
async def test_context_uses_select_only_without_autoflush(database_session: AsyncSession) -> None:
    analysis_id, call_id = await seed(database_session)
    database_session.add(AdmetEndpointRecord())  # flush하면 NOT NULL 제약 위반인 pending 객체
    statements: list[str] = []
    connection = await database_session.connection()

    def capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(connection.sync_connection, "before_cursor_execute", capture)
    try:
        result = await AdmetContextBuilder(database_session).build(
            analysis_id=analysis_id,
            query=AdmetContextQuery(source_tool_call_id=call_id, endpoint_ids=("AMES",)),
        )
        assert result.rows[0].value == 0.21
        assert len(statements) == 4 and all(
            sql.lstrip().upper().startswith("SELECT") for sql in statements
        )
        assert not any("model_artifacts" in sql or "dataset_size" in sql for sql in statements)
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", capture)
        await database_session.rollback()


@pytest.mark.asyncio
async def test_unsupported_manifest_is_rejected(database_session: AsyncSession) -> None:
    analysis_id, call_id = await seed(database_session)
    await database_session.execute(
        update(AdmetModelManifestRecord).values(schema_version="unknown")
    )
    await database_session.commit()
    with pytest.raises(AdmetContextUnavailable, match="unsupported_manifest"):
        await AdmetContextBuilder(database_session).build(
            analysis_id=analysis_id,
            query=AdmetContextQuery(source_tool_call_id=call_id, endpoint_ids=("AMES",)),
        )


@pytest.mark.asyncio
async def test_oversized_context_is_rejected_without_dropping_limitations(
    database_session: AsyncSession,
) -> None:
    analysis_id, call_id = await seed(database_session)
    await database_session.execute(
        update(AdmetManifestLimitationRecord).values(limitation="x" * 32768)
    )
    await database_session.commit()
    with pytest.raises(AdmetContextUnavailable, match="context_too_large"):
        await AdmetContextBuilder(database_session).build(
            analysis_id=analysis_id,
            query=AdmetContextQuery(source_tool_call_id=call_id, endpoint_ids=("AMES",)),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1.5, True])
async def test_context_rejects_invalid_classification_values(
    database_session: AsyncSession, value: object
) -> None:
    analysis_id, call_id = await seed(database_session)
    result = await AdmetContextBuilder(database_session).build(
        analysis_id=analysis_id,
        query=AdmetContextQuery(source_tool_call_id=call_id, endpoint_ids=("AMES",)),
    )
    payload = result.model_dump(mode="json")
    payload["rows"][0][4] = value
    with pytest.raises(ValidationError):
        AdmetContext.model_validate(payload)
