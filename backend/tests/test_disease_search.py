from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response
from pydantic import SecretStr

from evidrug_api.auth.dependencies import require_authenticated_session
from evidrug_api.auth.rate_limit import RateLimitDecision
from evidrug_api.auth.service import AuthenticatedSession
from evidrug_api.config import Settings
from evidrug_api.disease_search.client import (
    OpenTargetsDiseaseSearcher,
    OpenTargetsUnavailable,
)
from evidrug_api.disease_search.models import DiseaseCandidate
from evidrug_api.main import create_app


class UnusedAuthAttemptLimiter:
    """질환 검색 테스트에서는 호출되지 않는 인증 시도 제한기."""

    async def consume(self, client_identifier: str) -> RateLimitDecision:
        return RateLimitDecision(allowed=True, retry_after_seconds=0)

    async def reset(self, client_identifier: str) -> None:
        return None


class FakeDiseaseSearcher:
    """API 테스트가 외부 네트워크 없이 제어하는 검색 결과."""

    def __init__(
        self,
        candidates: list[DiseaseCandidate] | None = None,
        error: OpenTargetsUnavailable | None = None,
    ) -> None:
        self.candidates = candidates or []
        self.error = error
        self.received_query: str | None = None
        self.received_limit: int | None = None

    async def search(self, query: str, limit: int) -> list[DiseaseCandidate]:
        self.received_query = query
        self.received_limit = limit
        if self.error is not None:
            raise self.error
        return self.candidates


def build_disease_search_app(searcher: FakeDiseaseSearcher) -> FastAPI:
    """인증 성공 상태와 가짜 검색기를 사용하는 격리된 앱을 만든다."""

    app = create_app(
        settings=Settings(environment="test", disease_candidate_limit=5),
        auth_attempt_limiter=UnusedAuthAttemptLimiter(),
        disease_searcher=searcher,
    )
    app.dependency_overrides[require_authenticated_session] = lambda: AuthenticatedSession(
        expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    return app


@pytest.mark.asyncio
async def test_authenticated_search_returns_unconfirmed_open_targets_candidates() -> None:
    searcher = FakeDiseaseSearcher(
        candidates=[
            DiseaseCandidate(
                id="MONDO_0004975",
                name="Alzheimer disease",
                description="A progressive neurodegenerative disease.",
                relevance_score=5666.6562,
            )
        ]
    )
    app = build_disease_search_app(searcher)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
    ) as client:
        response = await client.post(
            "/api/v1/diseases/search",
            json={"query": "  Alzheimer disease  "},
        )

    assert response.status_code == 200
    assert response.json() == {
        "query": "Alzheimer disease",
        "candidates": [
            {
                "id": "MONDO_0004975",
                "name": "Alzheimer disease",
                "description": "A progressive neurodegenerative disease.",
                "relevance_score": 5666.6562,
            }
        ],
        "requires_confirmation": True,
        "source": "open_targets",
    }
    assert searcher.received_query == "Alzheimer disease"
    assert searcher.received_limit == 5


@pytest.mark.asyncio
async def test_search_rejects_non_english_input() -> None:
    app = build_disease_search_app(FakeDiseaseSearcher())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
    ) as client:
        response = await client.post(
            "/api/v1/diseases/search",
            json={"query": "알츠하이머병"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_requires_authentication() -> None:
    app = create_app(
        settings=Settings(
            environment="test",
            access_code_hash=SecretStr("unused-test-hash"),
            session_secret=SecretStr("test-session-secret-with-at-least-32-characters"),
        ),
        auth_attempt_limiter=UnusedAuthAttemptLimiter(),
        disease_searcher=FakeDiseaseSearcher(),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
    ) as client:
        response = await client.post(
            "/api/v1/diseases/search",
            json={"query": "Alzheimer disease"},
        )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_session"


@pytest.mark.asyncio
async def test_search_returns_not_found_when_there_are_no_candidates() -> None:
    app = build_disease_search_app(FakeDiseaseSearcher())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
    ) as client:
        response = await client.post(
            "/api/v1/diseases/search",
            json={"query": "unknown disease name"},
        )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "disease_candidates_not_found"


@pytest.mark.asyncio
async def test_search_returns_service_unavailable_for_provider_failure() -> None:
    searcher = FakeDiseaseSearcher(error=OpenTargetsUnavailable("provider failed"))
    app = build_disease_search_app(searcher)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
    ) as client:
        response = await client.post(
            "/api/v1/diseases/search",
            json={"query": "Alzheimer disease"},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "disease_search_unavailable"


@pytest.mark.asyncio
async def test_open_targets_client_parses_graphql_response() -> None:
    def handle_request(request: Request) -> Response:
        assert request.url == "https://open-targets.test/graphql"
        assert b"disease" in request.content
        return Response(
            200,
            json={
                "data": {
                    "search": {
                        "hits": [
                            {
                                "id": "MONDO_0004975",
                                "name": "Alzheimer disease",
                                "description": "Description",
                                "score": 100.5,
                            }
                        ]
                    }
                }
            },
        )

    async with httpx.AsyncClient(transport=MockTransport(handle_request)) as http_client:
        searcher = OpenTargetsDiseaseSearcher(
            http_client=http_client,
            graphql_url="https://open-targets.test/graphql",
        )
        candidates = await searcher.search("Alzheimer disease", limit=5)

    assert candidates == [
        DiseaseCandidate(
            id="MONDO_0004975",
            name="Alzheimer disease",
            description="Description",
            relevance_score=100.5,
        )
    ]


@pytest.mark.asyncio
async def test_open_targets_client_rejects_graphql_errors() -> None:
    def handle_request(_: Request) -> Response:
        return Response(200, json={"errors": [{"message": "Search failed"}]})

    async with httpx.AsyncClient(transport=MockTransport(handle_request)) as http_client:
        searcher = OpenTargetsDiseaseSearcher(
            http_client=http_client,
            graphql_url="https://open-targets.test/graphql",
        )
        with pytest.raises(OpenTargetsUnavailable):
            await searcher.search("Alzheimer disease", limit=5)
