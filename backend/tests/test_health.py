import pytest
from httpx import ASGITransport, AsyncClient

from evidrug_api.main import app


def test_application_reports_v1_release_version() -> None:
    assert app.version == "1.0.0"


@pytest.mark.asyncio
async def test_health_returns_service_status() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "EviDrug API",
        "environment": "local",
    }
