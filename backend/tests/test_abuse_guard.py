import asyncio
from collections.abc import AsyncIterator
from typing import Literal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic_settings import SettingsConfigDict
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from evidrug_api.api.abuse_guard import ApiAbuseGuard
from evidrug_api.config import Settings as RuntimeSettings
from evidrug_api.main import create_app


class Settings(RuntimeSettings):
    """테스트에서는 로컬 dotenv를 읽지 않는다."""

    model_config = SettingsConfigDict(env_file=None)


async def echo(scope: Scope, receive: Receive, send: Send) -> None:
    """보호 계층을 통과한 본문을 그대로 응답하는 테스트 앱."""
    message = await receive()
    await JSONResponse({"body": message.get("body", b"").decode()})(scope, receive, send)


@pytest.mark.asyncio
async def test_valid_body_reaches_app_unchanged() -> None:
    guard = ApiAbuseGuard(echo, Settings())
    async with AsyncClient(transport=ASGITransport(app=guard), base_url="http://test") as client:
        response = await client.post("/api/v1/diseases/search", json={"query": "cancer"})
    assert response.status_code == 200
    assert "cancer" in response.json()["body"]
    assert guard.active == 0


@pytest.mark.asyncio
async def test_global_limit_ignores_spoofed_ip_and_cookie_and_preserves_health() -> None:
    guard = ApiAbuseGuard(echo, Settings(api_total_limit=1))
    async with AsyncClient(transport=ASGITransport(app=guard), base_url="http://test") as client:
        assert (await client.get("/api/v1/auth/session")).status_code == 200
        response = await client.get(
            "/api/v1/auth/session",
            headers={"X-Forwarded-For": "203.0.113.1", "Cookie": "evidrug_session=changed"},
        )
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0
        assert response.headers["Cache-Control"] == "no-store"
        assert (await client.get("/api/v1/health")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["auth/session", "diseases/search", "analysis-inputs/validate", "analyses"]
)
async def test_route_limits_and_window_reset(path: str) -> None:
    now = [0.0]
    guard = ApiAbuseGuard(
        echo,
        Settings(
            api_login_limit=1,
            api_search_limit=1,
            api_validation_limit=1,
            api_analysis_limit=1,
        ),
        clock=lambda: now[0],
    )
    async with AsyncClient(transport=ASGITransport(app=guard), base_url="http://test") as client:
        assert (await client.post(f"/api/v1/{path}")).status_code == 200
        assert (await client.post(f"/api/v1/{path}/")).status_code == 429
        now[0] = 60.0
        assert (await client.post(f"/api/v1/{path}")).status_code == 200


@pytest.mark.asyncio
async def test_body_limit_counts_streamed_bytes_without_trusting_content_length() -> None:
    guard = ApiAbuseGuard(echo, Settings(api_max_body_bytes=1024))

    async def chunks() -> AsyncIterator[bytes]:
        yield b"a" * 800
        yield b"b" * 800

    async with AsyncClient(transport=ASGITransport(app=guard), base_url="http://test") as client:
        response = await client.post("/api/v1/diseases/search", content=chunks())
        assert response.status_code == 413
        assert guard.active == 0
        response = await client.post("/api/v1/diseases/search", content=b"a" * 1024)
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_concurrency_rejects_without_queueing_and_releases_slot() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_app(scope: Scope, receive: Receive, send: Send) -> None:
        started.set()
        await release.wait()
        await echo(scope, receive, send)

    guard = ApiAbuseGuard(slow_app, Settings(api_max_concurrent_requests=1))
    async with AsyncClient(transport=ASGITransport(app=guard), base_url="http://test") as client:
        task = asyncio.create_task(client.post("/api/v1/diseases/search"))
        await started.wait()
        try:
            response = await client.post("/api/v1/diseases/search")
            assert response.status_code == 503
            assert response.headers["Retry-After"] == "1"
        finally:
            release.set()
            await task
        assert guard.active == 0
        assert (await client.post("/api/v1/diseases/search")).status_code == 200


@pytest.mark.asyncio
async def test_exception_does_not_leak_concurrency_slot() -> None:
    async def broken_app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("test failure")

    guard = ApiAbuseGuard(broken_app, Settings())
    async with AsyncClient(transport=ASGITransport(app=guard), base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="test failure"):
            await client.post("/api/v1/diseases/search")
    assert guard.active == 0


@pytest.mark.asyncio
async def test_cancellation_and_disconnect_release_slot() -> None:
    started = asyncio.Event()

    async def waiting_app(scope: Scope, receive: Receive, send: Send) -> None:
        started.set()
        await asyncio.Event().wait()

    guard = ApiAbuseGuard(waiting_app, Settings(api_max_concurrent_requests=1))
    async with AsyncClient(transport=ASGITransport(app=guard), base_url="http://test") as client:
        task = asyncio.create_task(client.post("/api/v1/diseases/search"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert guard.active == 0

    async def disconnect() -> Message:
        return {"type": "http.disconnect"}

    async def unexpected_send(message: Message) -> None:
        pytest.fail("disconnected request must not send a response")

    await guard(
        {"type": "http", "method": "POST", "path": "/api/v1/auth/session"},
        disconnect,
        unexpected_send,
    )
    assert guard.active == 0


@pytest.mark.asyncio
async def test_body_timeout_returns_408_and_releases_slot() -> None:
    guard = ApiAbuseGuard(echo, Settings(api_body_timeout_seconds=0.01))
    messages: list[Message] = []

    async def receive() -> Message:
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await guard({"type": "http", "method": "POST", "path": "/api/v1/auth/session"}, receive, send)
    assert messages[0]["status"] == 408
    assert guard.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["staging", "production"])
async def test_public_deployment_hides_docs(environment: Literal["staging", "production"]) -> None:
    settings = Settings(environment=environment)
    app = create_app(settings=settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ["/api/v1/docs", "/api/v1/openapi.json", "/redoc"]:
            assert (await client.get(path)).status_code == 404
        assert (await client.get("/api/v1/health")).status_code == 200


@pytest.mark.asyncio
async def test_local_docs_and_cors_on_guard_rejections() -> None:
    app = create_app(settings=Settings(api_total_limit=1))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/docs")).status_code == 200
        response = await client.get("/api/v1/docs", headers={"Origin": "http://localhost:5173"})
        assert response.status_code == 429
        assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
