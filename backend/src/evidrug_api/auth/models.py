from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CreateSessionRequest(BaseModel):
    """공용 접근 코드로 세션을 만들 때 받는 요청."""

    model_config = ConfigDict(extra="forbid")

    access_code: str = Field(min_length=1, max_length=1024)


class SessionResponse(BaseModel):
    """인증된 세션의 공개 가능한 상태."""

    authenticated: Literal[True] = True
    expires_at: datetime


class ErrorDetail(BaseModel):
    """클라이언트가 분기 처리할 수 있는 오류 정보."""

    code: str
    message: str


class ErrorResponse(BaseModel):
    """FastAPI HTTP 오류의 공통 응답 구조."""

    detail: ErrorDetail
