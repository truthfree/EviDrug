from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from evidrug_api.auth.dependencies import (
    get_auth_attempt_limiter,
    get_auth_service,
    require_allowed_origin,
    require_authenticated_session,
)
from evidrug_api.auth.models import CreateSessionRequest, ErrorResponse, SessionResponse
from evidrug_api.auth.rate_limit import (
    AuthAttemptLimiter,
    RateLimitDecision,
    RateLimitStoreUnavailable,
)
from evidrug_api.auth.service import (
    AuthConfigurationError,
    AuthenticatedSession,
    AuthService,
    InvalidVisitorToken,
)
from evidrug_api.config import Settings, get_runtime_settings

router = APIRouter(prefix="/auth", tags=["authentication"])

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "The access code or session is invalid."},
    403: {"model": ErrorResponse, "description": "The request origin is not allowed."},
    429: {"model": ErrorResponse, "description": "Too many authentication attempts."},
    503: {"model": ErrorResponse, "description": "Authentication is unavailable."},
}


@router.post(
    "/session",
    response_model=SessionResponse,
    responses=ERROR_RESPONSES,
    summary="Create an authenticated session",
)
async def create_session(
    payload: CreateSessionRequest,
    request: Request,
    response: Response,
    _: Annotated[None, Depends(require_allowed_origin)],
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    attempt_limiter: Annotated[AuthAttemptLimiter, Depends(get_auth_attempt_limiter)],
) -> SessionResponse:
    """공용 접근 코드를 검증하고 보안 속성이 적용된 세션 쿠키를 발급한다."""

    client_identifier = _client_identifier(request)
    rate_limit = await _consume_attempt(attempt_limiter, client_identifier)
    if not rate_limit.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "too_many_auth_attempts",
                "message": "Too many authentication attempts. Try again later.",
            },
            headers={"Retry-After": str(rate_limit.retry_after_seconds)},
        )

    try:
        access_code_is_valid = auth_service.verify_access_code(payload.access_code)
    except AuthConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "auth_unavailable", "message": "Authentication is unavailable."},
        ) from error

    if not access_code_is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_access_code", "message": "The access code is invalid."},
        )

    await _reset_attempts(attempt_limiter, client_identifier)
    token, session = auth_service.create_session()
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        expires=session.expires_at,
        path="/",
        secure=settings.use_secure_cookies,
        httponly=True,
        samesite="lax",
    )
    _set_visitor_cookie(request, response, settings, auth_service, refresh=True)
    return SessionResponse(expires_at=session.expires_at)


@router.get(
    "/session",
    response_model=SessionResponse,
    responses={401: ERROR_RESPONSES[401], 503: ERROR_RESPONSES[503]},
    summary="Read the current authenticated session",
)
def read_session(
    request: Request,
    response: Response,
    session: Annotated[AuthenticatedSession, Depends(require_authenticated_session)],
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> SessionResponse:
    """현재 요청의 세션 상태와 만료 시각을 반환한다."""

    response.headers["Cache-Control"] = "no-store"
    _set_visitor_cookie(request, response, settings, auth_service, refresh=False)
    return SessionResponse(expires_at=session.expires_at)


@router.delete(
    "/session",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={403: ERROR_RESPONSES[403]},
    summary="Delete the current authenticated session",
)
def delete_session(
    response: Response,
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    _: Annotated[None, Depends(require_allowed_origin)],
) -> Response:
    """단기 인증 쿠키만 제거하며 브라우저별 분석 기록 쿠키는 유지한다."""

    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        secure=settings.use_secure_cookies,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


def _set_visitor_cookie(
    request: Request,
    response: Response,
    settings: Settings,
    auth_service: AuthService,
    *,
    refresh: bool,
) -> None:
    """서명된 브라우저 ID를 발급하고 로그인 시에는 같은 ID의 만료만 갱신한다."""

    visitor_id: str | None = None
    existing = request.cookies.get(settings.visitor_cookie_name)
    if existing:
        try:
            visitor_id = auth_service.read_visitor_token(existing)
        except InvalidVisitorToken:
            pass
    if visitor_id is not None and not refresh:
        return

    token, _ = auth_service.create_visitor_token(visitor_id)
    response.set_cookie(
        key=settings.visitor_cookie_name,
        value=token,
        max_age=settings.visitor_ttl_seconds,
        path="/",
        secure=settings.use_secure_cookies,
        httponly=True,
        samesite="lax",
    )


async def _consume_attempt(
    attempt_limiter: AuthAttemptLimiter,
    client_identifier: str,
) -> RateLimitDecision:
    try:
        return await attempt_limiter.consume(client_identifier)
    except RateLimitStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "auth_unavailable", "message": "Authentication is unavailable."},
        ) from error


async def _reset_attempts(
    attempt_limiter: AuthAttemptLimiter,
    client_identifier: str,
) -> None:
    try:
        await attempt_limiter.reset(client_identifier)
    except RateLimitStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "auth_unavailable", "message": "Authentication is unavailable."},
        ) from error


def _client_identifier(request: Request) -> str:
    if request.client is None:
        return "unknown-client"
    return request.client.host
