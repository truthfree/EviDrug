"""SQLAlchemy 비동기 연결과 요청 단위 세션 경계."""

from collections.abc import AsyncIterator
from typing import cast

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(AsyncAttrs, DeclarativeBase):
    """애플리케이션 ORM 모델이 공유하는 declarative base."""


DatabaseSessionFactory = async_sessionmaker[AsyncSession]


def create_database_engine(database_url: str) -> AsyncEngine:
    """환경별 URL로 연결 풀을 만들되 실제 연결은 첫 사용까지 지연한다."""

    return create_async_engine(database_url, pool_pre_ping=True)


def create_database_session_factory(engine: AsyncEngine) -> DatabaseSessionFactory:
    """요청마다 독립된 세션을 만드는 factory를 구성한다."""

    return async_sessionmaker(engine, expire_on_commit=False)


async def get_database_session(request: Request) -> AsyncIterator[AsyncSession]:
    """현재 애플리케이션의 factory에서 요청 범위 세션을 제공한다."""

    factory = cast(DatabaseSessionFactory, request.app.state.database_session_factory)
    async with factory() as session:
        yield session
