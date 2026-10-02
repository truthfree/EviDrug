"""인증된 사용자를 위한 질환 후보 검색 API."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from evidrug_api.auth.dependencies import require_authenticated_session
from evidrug_api.auth.models import ErrorResponse
from evidrug_api.auth.service import AuthenticatedSession
from evidrug_api.config import Settings, get_runtime_settings
from evidrug_api.disease_search.client import DiseaseSearcher, OpenTargetsUnavailable
from evidrug_api.disease_search.dependencies import get_disease_searcher
from evidrug_api.disease_search.models import DiseaseSearchRequest, DiseaseSearchResponse

router = APIRouter(prefix="/diseases", tags=["disease search"])

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Authentication is required."},
    404: {"model": ErrorResponse, "description": "No matching disease was found."},
    503: {"model": ErrorResponse, "description": "Open Targets is unavailable."},
}


@router.post(
    "/search",
    response_model=DiseaseSearchResponse,
    responses=ERROR_RESPONSES,
    summary="Search Open Targets for disease candidates",
)
async def search_diseases(
    payload: DiseaseSearchRequest,
    _: Annotated[AuthenticatedSession, Depends(require_authenticated_session)],
    settings: Annotated[Settings, Depends(get_runtime_settings)],
    searcher: Annotated[DiseaseSearcher, Depends(get_disease_searcher)],
) -> DiseaseSearchResponse:
    """영어 검색어와 일치하는 Open Targets 질환 후보를 반환한다."""

    try:
        candidates = await searcher.search(payload.query, settings.disease_candidate_limit)
    except OpenTargetsUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "disease_search_unavailable",
                "message": "Disease search is temporarily unavailable.",
            },
        ) from error

    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "disease_candidates_not_found",
                "message": "No matching disease or phenotype was found.",
            },
        )

    return DiseaseSearchResponse(query=payload.query, candidates=candidates)
