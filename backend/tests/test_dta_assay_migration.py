import io
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from evidrug_api.config import Settings


def _config(backend: Path) -> Config:
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    config.set_main_option("path_separator", "os")
    return config


def test_all_revision_ids_fit_default_postgresql_version_column() -> None:
    backend = Path(__file__).resolve().parents[1]
    revisions = ScriptDirectory.from_config(_config(backend)).walk_revisions()

    assert all(len(revision.revision) <= 32 for revision in revisions)


def test_assay_migration_can_upgrade_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "assay-migration.db"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url=f"sqlite+aiosqlite:///{path}"
    )
    monkeypatch.setattr("evidrug_api.config.get_settings", lambda: settings)
    backend = Path(__file__).resolve().parents[1]
    config = _config(backend)

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{path}")
    try:
        assert {"dta_assay_queries", "dta_assay_evidence"} <= set(inspect(engine).get_table_names())
        query_columns = {
            column["name"] for column in inspect(engine).get_columns("dta_assay_queries")
        }
        assert "canonical_smiles_sha256" in query_columns
        assert "canonical_smiles" not in query_columns
        command.downgrade(config, "0012_add_potency_criterion")
        assert "dta_assay_queries" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        assert "dta_assay_evidence" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_assay_postgresql_migration_sql_compiles_without_database_access(
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

    command.upgrade(config, "0012_add_potency_criterion:head", sql=True)

    output = sql.getvalue()
    assert "CREATE TABLE dta_assay_queries" in output
    assert "CREATE TABLE dta_assay_evidence" in output
    assert "FOREIGN KEY(run_id) REFERENCES agent_runs (run_id) ON DELETE CASCADE" in output
