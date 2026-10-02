from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from evidrug_api.analysis_input.smiles import InvalidSmiles, RdkitSmilesParser
from evidrug_api.auth.dependencies import require_authenticated_session
from evidrug_api.auth.rate_limit import RateLimitDecision
from evidrug_api.auth.service import AuthenticatedSession
from evidrug_api.config import Settings
from evidrug_api.main import create_app


class UnusedAuthAttemptLimiter:
    """분석 입력 테스트에서는 호출되지 않는 인증 시도 제한기."""

    async def consume(self, client_identifier: str) -> RateLimitDecision:
        return RateLimitDecision(allowed=True, retry_after_seconds=0)

    async def reset(self, client_identifier: str) -> None:
        return None


def build_analysis_input_app() -> FastAPI:
    """인증 성공 상태와 실제 RDKit 파서를 사용하는 격리된 앱을 만든다."""

    app = create_app(
        settings=Settings(environment="test"),
        auth_attempt_limiter=UnusedAuthAttemptLimiter(),
        smiles_parser=RdkitSmilesParser(),
    )
    app.dependency_overrides[require_authenticated_session] = lambda: AuthenticatedSession(
        expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    return app


def specified_payload() -> dict[str, str]:
    """특정 타깃 평가 흐름의 유효한 기본 요청을 만든다."""

    return {
        "disease_id": "MONDO_0004975",
        "disease_name": "Alzheimer disease",
        "target_mode": "specified",
        "target_name": "BACE1",
        "smiles": "C(C)O",
    }


@pytest.mark.asyncio
async def test_validate_specified_target_and_canonicalize_smiles() -> None:
    app = build_analysis_input_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
    ) as client:
        response = await client.post(
            "/api/v1/analysis-inputs/validate",
            json=specified_payload(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "disease_id": "MONDO_0004975",
        "disease_name": "Alzheimer disease",
        "target_mode": "specified",
        "target_name": "BACE1",
        "original_smiles": "C(C)O",
        "canonical_smiles": "CCO",
        "potency_criterion": None,
    }


@pytest.mark.asyncio
async def test_validate_preserves_analysis_scoped_potency_criterion() -> None:
    app = build_analysis_input_app()
    payload = specified_payload() | {
        "potency_criterion": {"endpoint": "Kd", "maximum_value": 100, "unit": "nM"}
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
    ) as client:
        response = await client.post("/api/v1/analysis-inputs/validate", json=payload)

    assert response.status_code == 200
    assert response.json()["potency_criterion"] == {
        "endpoint": "Kd",
        "maximum_value": 100.0,
        "unit": "nM",
    }


@pytest.mark.asyncio
async def test_validate_discovery_mode_without_target() -> None:
    app = build_analysis_input_app()
    payload = specified_payload()
    payload["target_mode"] = "discover"
    del payload["target_name"]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
    ) as client:
        response = await client.post(
            "/api/v1/analysis-inputs/validate",
            json=payload,
        )

    assert response.status_code == 200
    assert response.json()["target_mode"] == "discover"
    assert response.json()["target_name"] is None


@pytest.mark.asyncio
async def test_specified_mode_requires_target_name() -> None:
    app = build_analysis_input_app()
    payload = specified_payload()
    del payload["target_name"]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
    ) as client:
        response = await client.post(
            "/api/v1/analysis-inputs/validate",
            json=payload,
        )

    assert response.status_code == 422
    assert "target_name is required" in response.text


@pytest.mark.asyncio
async def test_discovery_mode_rejects_target_name() -> None:
    app = build_analysis_input_app()
    payload = specified_payload()
    payload["target_mode"] = "discover"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
    ) as client:
        response = await client.post(
            "/api/v1/analysis-inputs/validate",
            json=payload,
        )

    assert response.status_code == 422
    assert "target_name must not be provided" in response.text


@pytest.mark.asyncio
async def test_invalid_smiles_returns_readable_error() -> None:
    app = build_analysis_input_app()
    payload = specified_payload()
    payload["smiles"] = "this-is-not-smiles"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
    ) as client:
        response = await client.post(
            "/api/v1/analysis-inputs/validate",
            json=payload,
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "invalid_smiles",
            "message": "The SMILES input is not a valid molecule.",
        }
    }


@pytest.mark.asyncio
async def test_validate_input_requires_authentication() -> None:
    app = create_app(
        settings=Settings(
            environment="test",
            access_code_hash=SecretStr("unused-test-hash"),
            session_secret=SecretStr("test-session-secret-with-at-least-32-characters"),
        ),
        auth_attempt_limiter=UnusedAuthAttemptLimiter(),
        smiles_parser=RdkitSmilesParser(),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.test",
    ) as client:
        response = await client.post(
            "/api/v1/analysis-inputs/validate",
            json=specified_payload(),
        )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_session"


def test_rdkit_parser_rejects_invalid_smiles() -> None:
    parser = RdkitSmilesParser()

    with pytest.raises(InvalidSmiles):
        parser.canonicalize("C1CC")
