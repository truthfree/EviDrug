import io
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from evidrug_api.config import Settings


def test_trajectory_migration_can_upgrade_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "trajectory-migration.db"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url=f"sqlite+aiosqlite:///{path}"
    )
    monkeypatch.setattr("evidrug_api.config.get_settings", lambda: settings)
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    config.set_main_option("path_separator", "os")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{path}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "trajectory_episodes",
            "trajectory_steps",
            "trajectory_evaluations",
            "trajectory_preferences",
            "admet_observation_snapshots",
        } <= tables
        command.downgrade(config, "0007_agent_replay")
        assert "trajectory_episodes" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        assert "trajectory_episodes" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_trajectory_postgresql_migration_sql_compiles_without_database_access(
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
    command.upgrade(config, "0007_agent_replay:head", sql=True)
    output = sql.getvalue()
    assert "CREATE TABLE trajectory_episodes" in output
    assert "CREATE TABLE trajectory_steps" in output
    assert "CREATE TABLE trajectory_preferences" in output
    assert "CREATE TABLE admet_observation_snapshots" in output
