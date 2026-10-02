from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status

from evidrug_api.auth.rate_limit import AuthAttemptLimiter
from evidrug_api.auth.service import (
    AuthConfigurationError,
    AuthenticatedSession,
    AuthService,
    InvalidSessionToken,
    InvalidVisitorToken,
)
from evidrug_api.config import Settings, get_runtime_settings


def get_auth_attempt_limiter(request: Request) -> AuthAttemptLimiter:
    """애플리케이션 시작 시 구성한 인증 시도 제한기를 반환한다."""

    return cast(AuthAttemptLimiter, request.app.state.auth_attempt_limiter)


def get_auth_service(
    settings: Annotated[Settings, Depends(get_runtime_settings)],
) -> AuthService:
    """현재 설정으로 인증 서비스를 만들고 설정 오류를 안전한 HTTP 오류로 바꾼다."""

    try:
        return AuthService.from_settings(settings)
    except AuthConfigurationError as error:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "auth_unavailable",
            "Authentication is not configured.",
        ) from error


def require_allowed_origin(
    request: Request,
    settings: Annotated[Settings, Depends(get_runtime_settings)],
) -> None:
    """쿠키를 변경하는 요청이 허용된 프론트엔드에서 왔는지 확인한다."""

    origin = request.headers.get("origin")
    if origin is None and settings.environment in {"local", "test"}:
        return
    if origin not in settings.cors_origins:
        raise _http_error(
            status.HTTP_403_FORBIDDEN,
            "origin_not_allowed",
            "This request origin is not allowed.",
        )


def require_authenticated_session(
    request: Request,
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthenticatedSession:
    """요청 쿠키에서 유효한 분석 세션을 읽는다."""

    token = request.cookies.get(settings.session_cookie_name)
    if token is None:
        raise _unauthorized_session_error()

    try:
        return auth_service.read_session(token)
    except InvalidSessionToken as error:
        raise _unauthorized_session_error() from error


def read_optional_visitor_id(
    request: Request,
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> str | None:
    """유효한 브라우저 쿠키만 소유권 증거로 전달하고 잘못된 쿠키는 무시한다."""

    token = request.cookies.get(settings.visitor_cookie_name)
    if not token:
        return None
    try:
        return auth_service.read_visitor_token(token)
    except InvalidVisitorToken:
        return None


def _unauthorized_session_error() -> HTTPException:
    return _http_error(
        status.HTTP_401_UNAUTHORIZED,
        "invalid_session",
        "Authentication is required.",
    )


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )
