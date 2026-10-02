"""입력 검증, 멱등 저장과 queue 전달을 조정하는 분석 작업 서비스."""

import hashlib
import json
import logging
from typing import cast
from uuid import UUID

from evidrug_api.analysis_input.models import (
    AnalysisInputRequest,
    AnalysisInputResponse,
    PotencyCriterion,
    PotencyEndpoint,
)
from evidrug_api.analysis_input.smiles import SmilesParser
from evidrug_api.analysis_jobs.dispatcher import AnalysisDispatcher, AnalysisDispatchError
from evidrug_api.analysis_jobs.models import (
    AnalysisEventResponse,
    AnalysisResponse,
    AnalysisStageResponse,
    RecentAnalysesResponse,
    RecentAnalysisSummary,
    TargetPrioritizationSummary,
)
from evidrug_api.analysis_jobs.repository import AnalysisRepository
from evidrug_api.analysis_jobs.tables import AnalysisRecord

logger = logging.getLogger(__name__)


class AnalysisNotFound(Exception):
    """요청한 분석 ID가 저장소에 없음을 나타낸다."""


def fingerprint_session(session_token: str) -> str:
    """세션 원문을 저장하지 않고 멱등 범위를 구분한다."""

    return hashlib.sha256(session_token.encode("utf-8")).hexdigest()


def fingerprint_visitor(visitor_id: str) -> str:
    """브라우저 ID 원문을 DB에 남기지 않고 분석 소유권을 연결한다."""

    return hashlib.sha256(f"visitor:{visitor_id}".encode()).hexdigest()


async def create_analysis(
    payload: AnalysisInputRequest,
    session_token: str,
    idempotency_key: str,
    smiles_parser: SmilesParser,
    repository: AnalysisRepository,
    dispatcher: AnalysisDispatcher,
    visitor_id: str | None = None,
) -> AnalysisResponse:
    """검증된 분석을 저장하고 최초 생성일 때만 queue에 전달한다."""

    canonical_smiles = smiles_parser.canonicalize(payload.smiles)
    validated = AnalysisInputResponse(
        disease_id=payload.disease_id,
        disease_name=payload.disease_name,
        target_mode=payload.target_mode,
        target_name=payload.target_name,
        original_smiles=payload.smiles,
        canonical_smiles=canonical_smiles,
        potency_criterion=payload.potency_criterion,
    )
    record, created = await repository.create(
        validated,
        fingerprint_session(session_token),
        idempotency_key,
        fingerprint_visitor(visitor_id) if visitor_id is not None else None,
    )
    if not created:
        return to_response(record)

    try:
        await dispatcher.dispatch(record.id)
    except AnalysisDispatchError:
        logger.exception(
            "analysis queue dispatch failed",
            extra={"analysis_id": str(record.id)},
        )
        record = await repository.mark_dispatch_failed(record.id)
    return to_response(record)


async def read_owned_analysis(
    analysis_id: UUID,
    repository: AnalysisRepository,
    session_token: str,
    visitor_id: str | None = None,
) -> AnalysisRecord:
    """같은 인증 세션에서 생성한 일반 분석만 조회한다. 실패는 모두 not found다."""
    record = await repository.get(analysis_id)
    if record is None:
        raise AnalysisNotFound
    owned_by_session = record.session_fingerprint == fingerprint_session(session_token)
    owned_by_visitor = visitor_id is not None and record.visitor_fingerprint == fingerprint_visitor(
        visitor_id
    )
    if not (owned_by_session or owned_by_visitor):
        raise AnalysisNotFound
    if await repository.is_development_replay(analysis_id):
        raise AnalysisNotFound
    return record


async def read_analysis(
    analysis_id: UUID,
    repository: AnalysisRepository,
    session_token: str,
    visitor_id: str | None = None,
) -> AnalysisResponse:
    """저장된 분석과 단계 상태를 반환한다."""

    record = await read_owned_analysis(analysis_id, repository, session_token, visitor_id)
    target_output_json = await repository.get_latest_target_output_json(analysis_id)
    return to_response(
        record,
        target_prioritization=_target_prioritization(target_output_json),
    )


async def list_recent_analyses(
    repository: AnalysisRepository, session_token: str, visitor_id: str | None
) -> RecentAnalysesResponse:
    """같은 브라우저 또는 현재 세션에서 만든 최신 분석의 입력 요약만 반환한다."""

    rows = await repository.list_recent(
        fingerprint_session(session_token),
        fingerprint_visitor(visitor_id) if visitor_id is not None else None,
    )
    return RecentAnalysesResponse(
        items=[
            RecentAnalysisSummary(
                analysis_id=row.id,
                status=row.status,
                disease_name=row.disease_name,
                target_mode=row.target_mode,
                target_name=row.target_name,
                canonical_smiles=row.canonical_smiles,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )


def to_response(
    record: AnalysisRecord,
    *,
    target_prioritization: TargetPrioritizationSummary | None = None,
) -> AnalysisResponse:
    """영속 모델의 내부 멱등 정보를 제외하고 공개 응답을 구성한다."""

    return AnalysisResponse(
        analysis_id=record.id,
        status=record.status,
        input=AnalysisInputResponse(
            disease_id=record.disease_id,
            disease_name=record.disease_name,
            target_mode=record.target_mode,
            target_name=record.target_name,
            original_smiles=record.original_smiles,
            canonical_smiles=record.canonical_smiles,
            potency_criterion=(
                PotencyCriterion(
                    endpoint=PotencyEndpoint(record.potency_endpoint),
                    maximum_value=cast(float, record.potency_maximum_value),
                    unit=cast(str, record.potency_unit),
                )
                if record.potency_endpoint is not None
                else None
            ),
        ),
        stages=[AnalysisStageResponse.model_validate(stage) for stage in record.stages],
        events=[AnalysisEventResponse.model_validate(event) for event in record.events],
        target_prioritization=target_prioritization,
        error_code=record.error_code,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _target_prioritization(output_json: str | None) -> TargetPrioritizationSummary | None:
    """큰 sequence를 제외하고 version 2 Target 결과의 공개 필드만 투영한다."""
    if output_json is None:
        return None
    try:
        output = json.loads(output_json)
        result = output.get("result")
        if result is None or not isinstance(result, dict) or "primary" not in result:
            return None
        return TargetPrioritizationSummary.model_validate(result)
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        logger.warning("stored target output could not be projected")
        return None
