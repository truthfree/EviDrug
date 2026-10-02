from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from celery import Celery
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pwdlib import PasswordHash
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from evidrug_api.analysis_input.models import TargetMode
from evidrug_api.analysis_input.smiles import RdkitSmilesParser
from evidrug_api.analysis_jobs.dispatcher import (
    AnalysisDispatchError,
    CeleryAnalysisDispatcher,
)
from evidrug_api.analysis_jobs.models import AnalysisStatus
from evidrug_api.analysis_jobs.service import fingerprint_session
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.auth.dependencies import require_authenticated_session
from evidrug_api.auth.rate_limit import RateLimitDecision
from evidrug_api.auth.service import AuthenticatedSession, AuthService
from evidrug_api.config import Settings
from evidrug_api.database import Base
from evidrug_api.main import create_app


class UnusedAuthAttemptLimiter:
    """분석 작업 테스트에서는 로그인 endpoint를 호출하지 않는다."""

    async def consume(self, client_identifier: str) -> RateLimitDecision:
        return RateLimitDecision(allowed=True, retry_after_seconds=0)

    async def reset(self, client_identifier: str) -> None:
        return None


class RecordingDispatcher:
    """broker 없이 전달된 분석 ID와 실패 흐름을 재현한다."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.analysis_ids: list[UUID] = []

    async def dispatch(self, analysis_id: UUID) -> None:
        self.analysis_ids.append(analysis_id)
        if self.fail:
            raise AnalysisDispatchError("test broker is unavailable")


class RecordingCelery:
    """Celery adapter의 task 이름과 최소 인자를 기록한다."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, list[str]]] = []

    def send_task(self, name: str, args: list[str]) -> None:
        self.calls.append((name, args))
        if self.fail:
            raise ConnectionError("test transport failure")


class AnalysisTestContext:
    """테스트 앱과 교체 가능한 저장·queue 자원을 함께 보관한다."""

    def __init__(
        self,
        app: FastAPI,
        engine: AsyncEngine,
        dispatcher: RecordingDispatcher,
    ) -> None:
        self.app = app
        self.engine = engine
        self.dispatcher = dispatcher


async def build_analysis_app(
    *,
    fail_dispatch: bool = False,
    environment: Literal["local", "test", "staging", "production"] = "test",
    authenticated: bool = True,
) -> AnalysisTestContext:
    """SQLite 저장소와 기록용 dispatcher를 사용하는 격리 앱을 만든다."""

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    dispatcher = RecordingDispatcher(fail=fail_dispatch)
    settings = Settings(
        environment=environment,
        cors_origins=["https://allowed.test"],
        access_code_hash=SecretStr("unused-test-hash"),
        session_secret=SecretStr("test-session-secret-with-at-least-32-characters"),
    )
    app = create_app(
        settings=settings,
        auth_attempt_limiter=UnusedAuthAttemptLimiter(),
        smiles_parser=RdkitSmilesParser(),
        database_session_factory=session_factory,
        analysis_dispatcher=dispatcher,
    )
    if authenticated:
        app.dependency_overrides[require_authenticated_session] = lambda: AuthenticatedSession(
            expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
    return AnalysisTestContext(app, engine, dispatcher)


@pytest_asyncio.fixture
async def analysis_context() -> AsyncIterator[AnalysisTestContext]:
    context = await build_analysis_app()
    try:
        yield context
    finally:
        await context.engine.dispose()


def analysis_payload() -> dict[str, str]:
    """특정 타깃 평가 흐름의 유효한 기본 입력을 만든다."""

    return {
        "disease_id": "MONDO_0004975",
        "disease_name": "Alzheimer disease",
        "target_mode": "specified",
        "target_name": "BACE1",
        "smiles": "C(C)O",
    }


def request_headers(key: str = "analysis-request-001") -> dict[str, str]:
    return {"Idempotency-Key": key}


def visitor_cookie() -> str:
    service = AuthService(
        access_code_hash="unused-test-hash",
        session_secret="test-session-secret-with-at-least-32-characters",
        session_ttl_seconds=300,
    )
    token, _ = service.create_visitor_token()
    return token


@pytest.mark.asyncio
async def test_celery_dispatcher_sends_only_analysis_id_and_normalizes_errors() -> None:
    analysis_id = uuid4()
    celery = RecordingCelery()
    dispatcher = CeleryAnalysisDispatcher(cast(Celery, celery))

    await dispatcher.dispatch(analysis_id)

    assert celery.calls == [("evidrug.analysis.run", [str(analysis_id)])]

    failing_dispatcher = CeleryAnalysisDispatcher(cast(Celery, RecordingCelery(fail=True)))
    with pytest.raises(AnalysisDispatchError):
        await failing_dispatcher.dispatch(analysis_id)


@pytest.mark.asyncio
async def test_create_analysis_persists_queued_stages_and_dispatches_only_id(
    analysis_context: AnalysisTestContext,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=analysis_context.app),
        base_url="http://api.test",
        cookies={"evidrug_session": "session-one"},
    ) as client:
        response = await client.post(
            "/api/v1/analyses",
            json=analysis_payload(),
            headers=request_headers(),
        )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["input"]["canonical_smiles"] == "CCO"
    assert [stage["name"] for stage in body["stages"]] == [
        "target_hypothesis",
        "admet",
        "dta",
        "decision",
    ]
    assert {stage["status"] for stage in body["stages"]} == {"pending"}
    assert body["events"] == [
        {
            "event_type": "analysis_created",
            "status": "queued",
            "reason_code": None,
            "created_at": body["events"][0]["created_at"],
        }
    ]
    assert analysis_context.dispatcher.analysis_ids == [UUID(body["analysis_id"])]


@pytest.mark.asyncio
async def test_same_session_and_idempotency_key_returns_original_without_redispatch(
    analysis_context: AnalysisTestContext,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=analysis_context.app),
        base_url="http://api.test",
        cookies={"evidrug_session": "session-one"},
    ) as client:
        first = await client.post(
            "/api/v1/analyses", json=analysis_payload(), headers=request_headers()
        )
        changed_payload = analysis_payload()
        changed_payload["disease_name"] = "Changed input must not replace the original"
        second = await client.post(
            "/api/v1/analyses", json=changed_payload, headers=request_headers()
        )

    assert second.status_code == 202
    assert second.json()["analysis_id"] == first.json()["analysis_id"]
    assert second.json()["input"]["disease_name"] == "Alzheimer disease"
    assert len(analysis_context.dispatcher.analysis_ids) == 1


@pytest.mark.asyncio
async def test_same_idempotency_key_in_different_sessions_creates_separate_jobs(
    analysis_context: AnalysisTestContext,
) -> None:
    transport = ASGITransport(app=analysis_context.app)
    async with AsyncClient(
        transport=transport,
        base_url="http://api.test",
        cookies={"evidrug_session": "session-one"},
    ) as first_client:
        first = await first_client.post(
            "/api/v1/analyses", json=analysis_payload(), headers=request_headers()
        )
    async with AsyncClient(
        transport=transport,
        base_url="http://api.test",
        cookies={"evidrug_session": "session-two"},
    ) as second_client:
        second = await second_client.post(
            "/api/v1/analyses", json=analysis_payload(), headers=request_headers()
        )

    assert first.json()["analysis_id"] != second.json()["analysis_id"]
    assert len(analysis_context.dispatcher.analysis_ids) == 2


@pytest.mark.asyncio
async def test_recent_list_and_detail_follow_browser_identity_across_sessions(
    analysis_context: AnalysisTestContext,
) -> None:
    browser = visitor_cookie()
    other_browser = visitor_cookie()
    transport = ASGITransport(app=analysis_context.app)
    async with AsyncClient(
        transport=transport,
        base_url="http://api.test",
        cookies={"evidrug_session": "first-session", "evidrug_visitor": browser},
    ) as first_client:
        created = await first_client.post(
            "/api/v1/analyses", json=analysis_payload(), headers=request_headers()
        )
    analysis_id = created.json()["analysis_id"]

    async with AsyncClient(
        transport=transport,
        base_url="http://api.test",
        cookies={"evidrug_session": "second-session", "evidrug_visitor": browser},
    ) as returning_client:
        recent = await returning_client.get("/api/v1/analyses")
        detail = await returning_client.get(f"/api/v1/analyses/{analysis_id}")
        results = await returning_client.get(f"/api/v1/analyses/{analysis_id}/results")

    assert recent.status_code == detail.status_code == results.status_code == 200
    assert recent.headers["cache-control"] == "no-store"
    assert recent.json() == {
        "items": [
            {
                "analysis_id": analysis_id,
                "status": "queued",
                "disease_name": "Alzheimer disease",
                "target_mode": "specified",
                "target_name": "BACE1",
                "canonical_smiles": "CCO",
                "created_at": recent.json()["items"][0]["created_at"],
            }
        ]
    }
    assert len(analysis_context.dispatcher.analysis_ids) == 1

    async with AsyncClient(
        transport=transport,
        base_url="http://api.test",
        cookies={"evidrug_session": "other-session", "evidrug_visitor": other_browser},
    ) as other_client:
        private_list = await other_client.get("/api/v1/analyses")
        private_detail = await other_client.get(f"/api/v1/analyses/{analysis_id}")
        private_results = await other_client.get(f"/api/v1/analyses/{analysis_id}/results")

    assert private_list.json() == {"items": []}
    assert private_detail.status_code == private_results.status_code == 404


@pytest.mark.asyncio
async def test_real_logout_and_relogin_preserve_browser_analysis_history() -> None:
    context = await build_analysis_app(authenticated=False)
    access_code = "browser-history-test-code"
    context.app.state.settings.access_code_hash = SecretStr(
        PasswordHash.recommended().hash(access_code)
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=context.app), base_url="http://api.test"
        ) as client:
            login = await client.post(
                "/api/v1/auth/session",
                json={"access_code": access_code},
                headers={"Origin": "https://allowed.test"},
            )
            first_session_token = client.cookies["evidrug_session"]
            first_visitor_token = client.cookies["evidrug_visitor"]
            created = await client.post(
                "/api/v1/analyses", json=analysis_payload(), headers=request_headers()
            )
            await client.delete("/api/v1/auth/session", headers={"Origin": "https://allowed.test"})
            assert client.cookies.get("evidrug_visitor") == first_visitor_token
            relogin = await client.post(
                "/api/v1/auth/session",
                json={"access_code": access_code},
                headers={"Origin": "https://allowed.test"},
            )
            recent = await client.get("/api/v1/analyses")
            detail = await client.get(f"/api/v1/analyses/{created.json()['analysis_id']}/results")
    finally:
        await context.engine.dispose()

    assert login.status_code == relogin.status_code == 200
    assert created.status_code == 202
    assert client.cookies["evidrug_session"] != first_session_token
    assert recent.json()["items"][0]["analysis_id"] == created.json()["analysis_id"]
    assert detail.status_code == 200


@pytest.mark.asyncio
async def test_invalid_visitor_cookie_does_not_claim_another_session_history(
    analysis_context: AnalysisTestContext,
) -> None:
    browser = visitor_cookie()
    transport = ASGITransport(app=analysis_context.app)
    async with AsyncClient(
        transport=transport,
        base_url="http://api.test",
        cookies={"evidrug_session": "old-session", "evidrug_visitor": browser},
    ) as owner:
        created = await owner.post(
            "/api/v1/analyses", json=analysis_payload(), headers=request_headers()
        )
        legacy_list = await owner.get("/api/v1/analyses")

    async with AsyncClient(
        transport=transport,
        base_url="http://api.test",
        cookies={"evidrug_session": "new-session", "evidrug_visitor": "tampered"},
    ) as client:
        recent = await client.get("/api/v1/analyses")
        detail = await client.get(f"/api/v1/analyses/{created.json()['analysis_id']}")

    assert len(legacy_list.json()["items"]) == 1
    assert recent.json() == {"items": []}
    assert detail.status_code == 404


@pytest.mark.asyncio
async def test_recent_list_is_limited_to_twenty_newest_summaries(
    analysis_context: AnalysisTestContext,
) -> None:
    factory = async_sessionmaker(analysis_context.engine, expire_on_commit=False)
    async with factory() as session:
        for index in range(22):
            session.add(
                AnalysisRecord(
                    session_fingerprint=fingerprint_session("list-owner"),
                    idempotency_key=f"recent-{index}",
                    status=AnalysisStatus.QUEUED,
                    disease_id="MONDO_0004975",
                    disease_name=f"Disease {index}",
                    target_mode=TargetMode.DISCOVER,
                    target_name=None,
                    original_smiles="CCO",
                    canonical_smiles="CCO",
                    created_at=datetime(2026, 9, 14, tzinfo=UTC) + timedelta(minutes=index),
                )
            )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=analysis_context.app),
        base_url="http://api.test",
        cookies={"evidrug_session": "list-owner"},
    ) as client:
        response = await client.get("/api/v1/analyses")

    names = [item["disease_name"] for item in response.json()["items"]]
    assert response.status_code == 200
    assert len(names) == 20
    assert names == [f"Disease {index}" for index in range(21, 1, -1)]


@pytest.mark.asyncio
async def test_dispatch_failure_is_persisted_and_readable() -> None:
    context = await build_analysis_app(fail_dispatch=True)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=context.app),
            base_url="http://api.test",
            cookies={"evidrug_session": "session-one"},
        ) as client:
            created = await client.post(
                "/api/v1/analyses", json=analysis_payload(), headers=request_headers()
            )
            response = await client.get(f"/api/v1/analyses/{created.json()['analysis_id']}")
    finally:
        await context.engine.dispose()

    assert created.status_code == 202
    assert created.json()["status"] == "failed"
    assert created.json()["error_code"] == "queue_unavailable"
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert [event["event_type"] for event in response.json()["events"]] == [
        "analysis_created",
        "queue_dispatch_failed",
    ]


@pytest.mark.asyncio
async def test_invalid_smiles_is_rejected_before_persistence(
    analysis_context: AnalysisTestContext,
) -> None:
    payload = analysis_payload()
    payload["smiles"] = "not-a-smiles"
    async with AsyncClient(
        transport=ASGITransport(app=analysis_context.app),
        base_url="http://api.test",
        cookies={"evidrug_session": "session-one"},
    ) as client:
        response = await client.post("/api/v1/analyses", json=payload, headers=request_headers())

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_smiles"
    assert analysis_context.dispatcher.analysis_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected_code"),
    [
        ({}, "missing"),
        ({"Idempotency-Key": "invalid key"}, "invalid_idempotency_key"),
    ],
)
async def test_analysis_creation_requires_valid_idempotency_key(
    analysis_context: AnalysisTestContext,
    headers: dict[str, str],
    expected_code: str,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=analysis_context.app),
        base_url="http://api.test",
        cookies={"evidrug_session": "session-one"},
    ) as client:
        response = await client.post("/api/v1/analyses", json=analysis_payload(), headers=headers)

    assert response.status_code == 422
    if expected_code == "missing":
        assert response.json()["detail"][0]["type"] == "missing"
    else:
        assert response.json()["detail"]["code"] == expected_code
    assert analysis_context.dispatcher.analysis_ids == []


@pytest.mark.asyncio
async def test_read_unknown_analysis_returns_not_found(
    analysis_context: AnalysisTestContext,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=analysis_context.app),
        base_url="http://api.test",
        cookies={"evidrug_session": "session-one"},
    ) as client:
        response = await client.get(f"/api/v1/analyses/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "analysis_not_found"


@pytest.mark.asyncio
async def test_analysis_creation_requires_authentication() -> None:
    context = await build_analysis_app(authenticated=False)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=context.app),
            base_url="http://api.test",
        ) as client:
            response = await client.post(
                "/api/v1/analyses", json=analysis_payload(), headers=request_headers()
            )
    finally:
        await context.engine.dispose()

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_session"


@pytest.mark.asyncio
async def test_recent_list_requires_authentication() -> None:
    context = await build_analysis_app(authenticated=False)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=context.app), base_url="http://api.test"
        ) as client:
            response = await client.get("/api/v1/analyses")
    finally:
        await context.engine.dispose()

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_session"


@pytest.mark.asyncio
async def test_public_analysis_creation_rejects_untrusted_origin() -> None:
    context = await build_analysis_app(environment="staging")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=context.app),
            base_url="https://api.test",
            cookies={"evidrug_session": "session-one"},
        ) as client:
            response = await client.post(
                "/api/v1/analyses",
                json=analysis_payload(),
                headers={**request_headers(), "Origin": "https://attacker.test"},
            )
    finally:
        await context.engine.dispose()

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "origin_not_allowed"
