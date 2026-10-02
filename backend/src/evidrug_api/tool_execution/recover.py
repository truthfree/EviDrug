"""재시작 후 또는 주기적으로 만료된 도구 작업을 회수하는 진입점."""

import asyncio

from evidrug_api.config import get_settings
from evidrug_api.database import create_database_engine, create_database_session_factory
from evidrug_api.tool_execution.repository import ExecutionRepository


async def recover_expired_executions(database_url: str) -> int:
    """DB에서 만료된 running 호출만 실패로 전환하고 회수 수를 반환한다."""
    engine = create_database_engine(database_url)
    try:
        async with create_database_session_factory(engine)() as session:
            return await ExecutionRepository(session).recover_expired()
    finally:
        await engine.dispose()


def main() -> None:
    """운영자가 직접 실행할 때 원본 입력 없이 회수된 개수만 출력한다."""
    count = asyncio.run(recover_expired_executions(get_settings().database_url))
    print(f"Recovered tool executions: {count}")


if __name__ == "__main__":
    main()
