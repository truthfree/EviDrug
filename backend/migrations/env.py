"""Alembic이 애플리케이션 설정과 ORM metadata를 사용하는 환경."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from evidrug_api.admet import tables as admet_tables  # noqa: F401
from evidrug_api.analysis_jobs import tables  # noqa: F401
from evidrug_api.config import get_settings
from evidrug_api.ctoxpred2 import tables as ctoxpred2_tables  # noqa: F401
from evidrug_api.database import Base
from evidrug_api.dta import assay_tables as dta_assay_tables  # noqa: F401
from evidrug_api.dta import tables as dta_tables  # noqa: F401
from evidrug_api.orchestration import tables as orchestration_tables  # noqa: F401
from evidrug_api.tool_admission import tables as tool_admission_tables  # noqa: F401
from evidrug_api.tool_execution import tables as tool_execution_tables  # noqa: F401
from evidrug_api.trajectory import tables as trajectory_tables  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """데이터베이스 연결 없이 migration SQL을 생성한다."""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_sync_migrations(connection: object) -> None:
    """비동기 연결이 제공한 동기 facade에서 migration을 실행한다."""

    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """설정된 데이터베이스에 연결해 migration을 실행한다."""

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(run_sync_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
