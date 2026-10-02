"""실제 Agent 조립 경로가 OpenAI SDK 운영 설정을 보존하는지 검증한다."""

from typing import cast
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from evidrug_api.config import Settings
from evidrug_api.openai_gateway.client import DaconOpenAIClient, build_dacon_openai_client
from evidrug_api.orchestration import continuation
from evidrug_api.orchestration.replay_store import ReplayStore
from evidrug_api.target_hypothesis.runtime import build_target_hypothesis_agent


@pytest.mark.asyncio
async def test_target_runtime_applies_configured_sdk_retries() -> None:
    runtime = build_target_hypothesis_agent(
        Settings(
            _env_file=None,  # type: ignore[call-arg]  # BaseSettings의 dotenv 비활성화 인수
            openai_api_key=SecretStr("test-key"),
            openai_max_retries=2,
        )
    )

    assert runtime.openai_client._sdk.max_retries == 2
    await runtime.aclose()


@pytest.mark.asyncio
async def test_continuation_runtime_applies_configured_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[DaconOpenAIClient] = []

    def capture_client(settings: Settings) -> DaconOpenAIClient:
        client = build_dacon_openai_client(settings)
        clients.append(client)
        return client

    monkeypatch.setattr(
        "evidrug_api.orchestration.continuation.build_dacon_openai_client", capture_client
    )
    store = MagicMock(spec=ReplayStore)
    store.factory = MagicMock()
    executor = continuation.build_continuation_executor(
        cast(ReplayStore, store),
        Settings(
            _env_file=None,  # type: ignore[call-arg]  # BaseSettings의 dotenv 비활성화 인수
            openai_api_key=SecretStr("test-key"),
            openai_max_retries=2,
        ),
    )

    assert len(clients) == 1
    assert clients[0]._sdk.max_retries == 2
    await executor.aclose()
