from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pwdlib import PasswordHash
from pydantic import SecretStr

from evidrug_api.auth.hash_access_code import hash_access_code
from evidrug_api.auth.rate_limit import RateLimitDecision
from evidrug_api.auth.service import AuthService, InvalidSessionToken
from evidrug_api.config import Settings
from evidrug_api.main import create_app

TEST_ACCESS_CODE = "research-access-code"
ACCESS_CODE_HASH_VALUE = PasswordHash.recommended().hash(TEST_ACCESS_CODE)
TEST_SESSION_SECRET = "test-session-secret-with-at-least-32-characters"
TRUSTED_ORIGIN = "http://frontend.test"
ACCESS_CODE_HASH = SecretStr(ACCESS_CODE_HASH_VALUE)
SESSION_SECRET_VALUE = SecretStr(TEST_SESSION_SECRET)


class InMemoryAuthAttemptLimiter:
    """API 테스트가 Redis 없이 동일한 시도 제한 계약을 검증하게 한다."""

    def __init__(self, attempt_limit: int, window_seconds: int = 300) -> None:
        self._attempt_limit = attempt_limit
        self._window_seconds = window_seconds
        self._attempts: dict[str, int] = {}

    async def consume(self, client_identifier: str) -> RateLimitDecision:
        attempts = self._attempts.get(client_identifier, 0) + 1
        self._attempts[client_identifier] = attempts
        return RateLimitDecision(
            allowed=attempts <= self._attempt_limit,
            retry_after_seconds=self._window_seconds,
        )

    async def reset(self, client_identifier: str) -> None:
        self._attempts.pop(client_identifier, None)


def build_auth_app(
    *,
    attempt_limit: int = 5,
    environment: Literal["local", "test", "staging", "production"] = "test",
    cors_origins: list[str] | None = None,
    access_code_hash: SecretStr | None = ACCESS_CODE_HASH,
    session_secret: SecretStr | None = SESSION_SECRET_VALUE,
) -> FastAPI:
    """각 테스트가 필요한 인증 설정으로 격리된 앱을 만든다."""

    settings = Settings(
        environment=environment,
        cors_origins=cors_origins or [TRUSTED_ORIGIN],
        access_code_hash=access_code_hash,
        session_secret=session_secret,
        auth_attempt_limit=attempt_limit,
    )
    return create_app(
        settings=settings,
        auth_attempt_limiter=InMemoryAuthAttemptLimiter(attempt_limit=attempt_limit),
    )


@pytest.mark.asyncio
async def test_valid_access_code_creates_readable_session() -> None:
    app = build_auth_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://api.test") as client:
        create_response = await client.post(
            "/api/v1/auth/session",
            json={"access_code": TEST_ACCESS_CODE},
            headers={"Origin": TRUSTED_ORIGIN},
        )
        read_response = await client.get("/api/v1/auth/session")

    assert create_response.status_code == 200
    assert create_response.json()["authenticated"] is True
    assert create_response.headers["cache-control"] == "no-store"
    assert read_response.status_code == 200
    assert read_response.json() == create_response.json()
    assert read_response.headers["cache-control"] == "no-store"

    set_cookie = create_response.headers["set-cookie"]
    assert "evidrug_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Max-Age=28800" in set_cookie
    assert "Path=/" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" not in set_cookie
    assert create_response.cookies.get("evidrug_visitor") is not None


def test_sessions_created_in_the_same_second_are_distinct() -> None:
    fixed_time = datetime(2026, 9, 14, tzinfo=UTC)
    service = AuthService(
        access_code_hash=ACCESS_CODE_HASH_VALUE,
        session_secret=TEST_SESSION_SECRET,
        session_ttl_seconds=300,
        now=lambda: fixed_time,
    )

    first, _ = service.create_session()
    second, _ = service.create_session()

    assert first != second
    assert service.read_session(first).expires_at == service.read_session(second).expires_at


def test_expired_browser_identity_is_rejected() -> None:
    current_time = [datetime(2026, 9, 14, tzinfo=UTC)]
    service = AuthService(
        access_code_hash=ACCESS_CODE_HASH_VALUE,
        session_secret=TEST_SESSION_SECRET,
        session_ttl_seconds=300,
        visitor_ttl_seconds=24 * 60 * 60,
        now=lambda: current_time[0],
    )
    token, visitor_id = service.create_visitor_token()
    assert service.read_visitor_token(token) == visitor_id

    current_time[0] += timedelta(days=1, seconds=1)
    with pytest.raises(ValueError, match="expired"):
        service.read_visitor_token(token)


@pytest.mark.asyncio
async def test_browser_identity_survives_logout_and_new_login() -> None:
    app = build_auth_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
        first = await client.post(
            "/api/v1/auth/session",
            json={"access_code": TEST_ACCESS_CODE},
            headers={"Origin": TRUSTED_ORIGIN},
        )
        visitor_token = client.cookies.get("evidrug_visitor")
        await client.delete("/api/v1/auth/session", headers={"Origin": TRUSTED_ORIGIN})
        retained_token = client.cookies.get("evidrug_visitor")
        second = await client.post(
            "/api/v1/auth/session",
            json={"access_code": TEST_ACCESS_CODE},
            headers={"Origin": TRUSTED_ORIGIN},
        )

    assert first.status_code == second.status_code == 200
    assert visitor_token is not None
    assert retained_token == visitor_token
    service = AuthService.from_settings(app.state.settings)
    assert service.read_visitor_token(visitor_token) == service.read_visitor_token(
        second.cookies["evidrug_visitor"]
    )


@pytest.mark.asyncio
async def test_invalid_browser_cookie_is_replaced_after_login() -> None:
    app = build_auth_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
        client.cookies.set("evidrug_visitor", "tampered")
        response = await client.post(
            "/api/v1/auth/session",
            json={"access_code": TEST_ACCESS_CODE},
            headers={"Origin": TRUSTED_ORIGIN},
        )

    assert response.status_code == 200
    assert AuthService.from_settings(app.state.settings).read_visitor_token(
        response.cookies["evidrug_visitor"]
    )


@pytest.mark.asyncio
async def test_invalid_access_code_returns_generic_error() -> None:
    app = build_auth_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.post(
            "/api/v1/auth/session",
            json={"access_code": "incorrect-access-code"},
            headers={"Origin": TRUSTED_ORIGIN},
        )

    assert response.status_code == 401
    assert response.json() == {
        "detail": {
            "code": "invalid_access_code",
            "message": "The access code is invalid.",
        }
    }
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_repeated_authentication_attempts_are_limited() -> None:
    app = build_auth_app(attempt_limit=2)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://api.test") as client:
        for _ in range(2):
            response = await client.post(
                "/api/v1/auth/session",
                json={"access_code": "incorrect-access-code"},
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert response.status_code == 401

        limited_response = await client.post(
            "/api/v1/auth/session",
            json={"access_code": TEST_ACCESS_CODE},
            headers={"Origin": TRUSTED_ORIGIN},
        )

    assert limited_response.status_code == 429
    assert limited_response.headers["retry-after"] == "300"
    assert limited_response.json()["detail"]["code"] == "too_many_auth_attempts"


@pytest.mark.asyncio
async def test_untrusted_origin_cannot_create_session() -> None:
    app = build_auth_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.post(
            "/api/v1/auth/session",
            json={"access_code": TEST_ACCESS_CODE},
            headers={"Origin": "https://untrusted.example"},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "origin_not_allowed"


@pytest.mark.asyncio
async def test_production_requires_origin_for_cookie_changes() -> None:
    production_origin = "https://app.example.test"
    app = build_auth_app(environment="production", cors_origins=[production_origin])
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="https://api.example.test") as client:
        response = await client.post(
            "/api/v1/auth/session",
            json={"access_code": TEST_ACCESS_CODE},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "origin_not_allowed"


@pytest.mark.asyncio
async def test_tampered_session_cookie_is_rejected() -> None:
    app = build_auth_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://api.test") as client:
        client.cookies.set("evidrug_session", "tampered-token")
        response = await client.get("/api/v1/auth/session")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_session"


@pytest.mark.asyncio
async def test_logout_removes_session_cookie() -> None:
    app = build_auth_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://api.test") as client:
        await client.post(
            "/api/v1/auth/session",
            json={"access_code": TEST_ACCESS_CODE},
            headers={"Origin": TRUSTED_ORIGIN},
        )
        logout_response = await client.delete(
            "/api/v1/auth/session",
            headers={"Origin": TRUSTED_ORIGIN},
        )
        read_response = await client.get("/api/v1/auth/session")

    assert logout_response.status_code == 204
    assert logout_response.headers["cache-control"] == "no-store"
    assert read_response.status_code == 401


@pytest.mark.asyncio
async def test_missing_authentication_secrets_return_service_unavailable() -> None:
    app = build_auth_app(access_code_hash=None, session_secret=None)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.post(
            "/api/v1/auth/session",
            json={"access_code": TEST_ACCESS_CODE},
            headers={"Origin": TRUSTED_ORIGIN},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "auth_unavailable"


@pytest.mark.asyncio
async def test_production_session_cookie_requires_https() -> None:
    production_origin = "https://app.example.test"
    app = build_auth_app(environment="production", cors_origins=[production_origin])
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="https://api.example.test") as client:
        response = await client.post(
            "/api/v1/auth/session",
            json={"access_code": TEST_ACCESS_CODE},
            headers={"Origin": production_origin},
        )

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all("Secure" in cookie and "HttpOnly" in cookie for cookie in cookies)


def test_expired_session_token_is_rejected() -> None:
    current_time = [datetime(2026, 9, 14, tzinfo=UTC)]
    service = AuthService(
        access_code_hash=ACCESS_CODE_HASH_VALUE,
        session_secret=TEST_SESSION_SECRET,
        session_ttl_seconds=300,
        now=lambda: current_time[0],
    )
    token, _ = service.create_session()
    current_time[0] += timedelta(seconds=301)

    with pytest.raises(InvalidSessionToken, match="expired"):
        service.read_session(token)


def test_access_code_hash_helper_rejects_short_codes() -> None:
    with pytest.raises(ValueError, match="at least 12"):
        hash_access_code("too-short")


def test_access_code_hash_helper_creates_verifiable_argon2_hash() -> None:
    generated_hash = hash_access_code(TEST_ACCESS_CODE)

    assert PasswordHash.recommended().verify(TEST_ACCESS_CODE, generated_hash) is True
    assert TEST_ACCESS_CODE not in generated_hash
