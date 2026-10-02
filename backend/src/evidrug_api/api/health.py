from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from evidrug_api.config import Settings, get_runtime_settings

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    environment: str


@router.get("/health", response_model=HealthResponse, summary="Check API process health")
def health(settings: Annotated[Settings, Depends(get_runtime_settings)]) -> HealthResponse:
    return HealthResponse(service=settings.app_name, environment=settings.environment)
