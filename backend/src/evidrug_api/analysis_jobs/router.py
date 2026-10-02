"""인증된 사용자의 분석 작업 생성과 상태 조회 API."""

import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.analysis_input.dependencies import get_smiles_parser
from evidrug_api.analysis_input.models import AnalysisInputRequest
from evidrug_api.analysis_input.smiles import InvalidSmiles, SmilesParser
from evidrug_api.analysis_jobs.dependencies import get_analysis_dispatcher
from evidrug_api.analysis_jobs.dispatcher import AnalysisDispatcher
from evidrug_api.analysis_jobs.models import AnalysisResponse, RecentAnalysesResponse
from evidrug_api.analysis_jobs.repository import AnalysisRepository
from evidrug_api.analysis_jobs.result_models import SpecialistResultsResponse
from evidrug_api.analysis_jobs.results import read_specialist_results
from evidrug_api.analysis_jobs.service import (
    AnalysisNotFound,
    create_analysis,
    list_recent_analyses,
    read_analysis,
)
from evidrug_api.auth.dependencies import (
    read_optional_visitor_id,
    require_allowed_origin,
    require_authenticated_session,
)
from evidrug_api.auth.models import ErrorResponse
from evidrug_api.auth.service import AuthenticatedSession
from evidrug_api.config import Settings, get_runtime_settings
from evidrug_api.database import get_database_session

router = APIRouter(prefix="/analyses", tags=["analyses"])

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Authentication is required."},
    403: {"model": ErrorResponse, "description": "The request origin is not allowed."},
    404: {"model": ErrorResponse, "description": "The analysis does not exist."},
    422: {"model": ErrorResponse, "description": "The analysis input is invalid."},
}
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")


@router.post(
    "",
    response_model=AnalysisResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
    summary="Create a persistent analysis job",
)
async def create_analysis_job(
    payload: AnalysisInputRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    _: Annotated[AuthenticatedSession, Depends(require_authenticated_session)],
    _origin: Annotated[None, Depends(require_allowed_origin)],
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    smiles_parser: Annotated[SmilesParser, Depends(get_smiles_parser)],
    dispatcher: Annotated[AnalysisDispatcher, Depends(get_analysis_dispatcher)],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    visitor_id: Annotated[str | None, Depends(read_optional_visitor_id)],
) -> AnalysisResponse:
    """입력을 다시 검증해 저장하고 작업 ID만 비동기 queue에 전달한다."""

    if IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_idempotency_key",
                "message": "Idempotency-Key contains unsupported characters.",
            },
        )
    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token is None:  # require_authenticated_session already rejects this case
        raise RuntimeError("authenticated request is missing its session cookie")
    try:
        return await create_analysis(
            payload=payload,
            session_token=session_token,
            idempotency_key=idempotency_key,
            smiles_parser=smiles_parser,
            repository=AnalysisRepository(session),
            dispatcher=dispatcher,
            visitor_id=visitor_id,
        )
    except InvalidSmiles as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_smiles",
                "message": "The SMILES input is not a valid molecule.",
            },
        ) from error


@router.get(
    "",
    response_model=RecentAnalysesResponse,
    responses={401: ERROR_RESPONSES[401]},
    summary="List recent analyses owned by this browser without inference",
)
async def list_analysis_jobs(
    request: Request,
    response: Response,
    _: Annotated[AuthenticatedSession, Depends(require_authenticated_session)],
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    visitor_id: Annotated[str | None, Depends(read_optional_visitor_id)],
) -> RecentAnalysesResponse:
    """입력 요약만 최신순으로 읽고 상세·provider 결과는 요청하지 않는다."""

    response.headers["Cache-Control"] = "no-store"
    return await list_recent_analyses(
        AnalysisRepository(session), _session_token(request, settings), visitor_id
    )


@router.get(
    "/{analysis_id}",
    response_model=AnalysisResponse,
    responses=ERROR_RESPONSES,
    summary="Read analysis job status",
)
async def read_analysis_job(
    analysis_id: UUID,
    request: Request,
    response: Response,
    _: Annotated[AuthenticatedSession, Depends(require_authenticated_session)],
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    visitor_id: Annotated[str | None, Depends(read_optional_visitor_id)],
) -> AnalysisResponse:
    """PostgreSQL을 기준으로 분석과 각 단계의 현재 상태를 조회한다."""

    response.headers["Cache-Control"] = "no-store"
    try:
        return await read_analysis(
            analysis_id, AnalysisRepository(session), _session_token(request, settings), visitor_id
        )
    except AnalysisNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "analysis_not_found", "message": "The analysis does not exist."},
        ) from error


def _session_token(request: Request, settings: Settings) -> str:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_session", "message": "Authentication is required."},
        )
    return token


@router.get(
    "/{analysis_id}/results",
    response_model=SpecialistResultsResponse,
    responses=ERROR_RESPONSES,
    summary="Read stored specialist results without new inference",
)
async def read_analysis_results(
    analysis_id: UUID,
    request: Request,
    response: Response,
    _: Annotated[AuthenticatedSession, Depends(require_authenticated_session)],
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    visitor_id: Annotated[str | None, Depends(read_optional_visitor_id)],
) -> SpecialistResultsResponse:
    """현재 세션·동일 브라우저 소유의 결과만 공개하며 개발 replay는 제외한다."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return await read_specialist_results(
            analysis_id, AnalysisRepository(session), _session_token(request, settings), visitor_id
        )
    except AnalysisNotFound as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "analysis_not_found", "message": "The analysis does not exist."},
        ) from error
