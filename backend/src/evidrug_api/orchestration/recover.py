"""만료된 orchestration 실행을 주기적으로 실패 처리하는 진입점."""

from evidrug_api.database import create_database_engine, create_database_session_factory
from evidrug_api.orchestration.repository import OrchestrationRepository


async def recover_expired_analyses(database_url: str) -> int:
    """독립 session에서 만료 분석을 회수하고 연결을 정리한다."""
    engine = create_database_engine(database_url)
    try:
        async with create_database_session_factory(engine)() as session:
            return await OrchestrationRepository(session).recover_expired()
    finally:
        await engine.dispose()
