"""인증된 사용자의 분석 입력을 검증하는 API."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from evidrug_api.analysis_input.dependencies import get_smiles_parser
from evidrug_api.analysis_input.models import AnalysisInputRequest, AnalysisInputResponse
from evidrug_api.analysis_input.smiles import InvalidSmiles, SmilesParser
from evidrug_api.auth.dependencies import require_authenticated_session
from evidrug_api.auth.models import ErrorResponse
from evidrug_api.auth.service import AuthenticatedSession

router = APIRouter(prefix="/analysis-inputs", tags=["analysis input"])

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Authentication is required."},
    422: {"model": ErrorResponse, "description": "The analysis input is invalid."},
}


@router.post(
    "/validate",
    response_model=AnalysisInputResponse,
    responses=ERROR_RESPONSES,
    summary="Validate target mode and SMILES input",
)
def validate_analysis_input(
    payload: AnalysisInputRequest,
    _: Annotated[AuthenticatedSession, Depends(require_authenticated_session)],
    smiles_parser: Annotated[SmilesParser, Depends(get_smiles_parser)],
) -> AnalysisInputResponse:
    """타깃 선택 규칙을 확인하고 SMILES를 canonical 형식으로 변환한다."""

    try:
        canonical_smiles = smiles_parser.canonicalize(payload.smiles)
    except InvalidSmiles as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_smiles",
                "message": "The SMILES input is not a valid molecule.",
            },
        ) from error

    return AnalysisInputResponse(
        disease_id=payload.disease_id,
        disease_name=payload.disease_name,
        target_mode=payload.target_mode,
        target_name=payload.target_name,
        original_smiles=payload.smiles,
        canonical_smiles=canonical_smiles,
        potency_criterion=payload.potency_criterion,
    )
