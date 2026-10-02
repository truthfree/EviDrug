import io
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, inspect, text

from evidrug_api.config import Settings


def test_replay_migration_preserves_old_runs_and_can_be_reapplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """기존 0006 DB의 출력은 유지하고 새 입력 snapshot은 NULL로 추가한다."""
    path = tmp_path / "migration.db"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url=f"sqlite+aiosqlite:///{path}"
    )
    monkeypatch.setattr("evidrug_api.config.get_settings", lambda: settings)
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    config.set_main_option("path_separator", "os")
    command.upgrade(config, "0006_analysis_orchestration")
    engine = create_engine(f"sqlite:///{path}")
    analysis_id, run_id = uuid4().hex, uuid4().hex
    original = '{"immutable_original":true}'
    now = datetime.now(UTC)
    try:
        metadata = MetaData()
        metadata.reflect(bind=engine)
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["analyses"]
                .insert()
                .values(
                    id=analysis_id,
                    session_fingerprint="a" * 64,
                    idempotency_key=analysis_id,
                    status="COMPLETED",
                    disease_id="MONDO_0007254",
                    disease_name="breast cancer",
                    target_mode="SPECIFIED",
                    target_name="CDK4",
                    original_smiles="test-only",
                    canonical_smiles="test-only",
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                metadata.tables["agent_runs"]
                .insert()
                .values(
                    run_id=run_id,
                    analysis_id=analysis_id,
                    agent_name="target_hypothesis",
                    attempt=1,
                    status="completed",
                    input_sha256="a" * 64,
                    output_json=original,
                    started_at=now,
                    finished_at=now,
                )
            )
        command.upgrade(config, "head")
        assert "agent_replays" in inspect(engine).get_table_names()
        assert next(
            column
            for column in inspect(engine).get_columns("agent_runs")
            if column["name"] == "input_json"
        )["nullable"]
        with engine.connect() as connection:
            row = connection.execute(text("SELECT output_json, input_json FROM agent_runs")).one()
            assert row == (original, None)
        command.downgrade(config, "0006_analysis_orchestration")
        assert "agent_replays" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT output_json FROM agent_runs")).scalar_one()
                == original
            )
    finally:
        engine.dispose()


def test_replay_postgresql_migration_sql_compiles_without_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url="postgresql+psycopg://evidrug@localhost/evidrug"
    )
    monkeypatch.setattr("evidrug_api.config.get_settings", lambda: settings)
    backend = Path(__file__).resolve().parents[1]
    sql = io.StringIO()
    config = Config(str(backend / "alembic.ini"), output_buffer=sql)
    config.set_main_option("script_location", str(backend / "migrations"))
    config.set_main_option("path_separator", "os")
    command.upgrade(config, "0006_analysis_orchestration:head", sql=True)
    assert "ALTER TABLE agent_runs ADD COLUMN input_json TEXT" in sql.getvalue()
    assert "CREATE TABLE agent_replays" in sql.getvalue()
    assert "FOREIGN KEY(source_run_id) REFERENCES agent_runs (run_id)" in sql.getvalue()
