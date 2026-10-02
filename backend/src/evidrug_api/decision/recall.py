"""Decision이 도구명이 아닌 근거 공백을 요청하는 폐쇄형 계약."""

from typing import Literal

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.execution_contracts.common import ContractModel
from evidrug_api.execution_contracts.recall import DecisionRecallRequest, RecallEvidenceGapKind

EvidenceGapKind = RecallEvidenceGapKind
__all__ = ["DecisionRecallRequest", "EvidenceGapKind", "RecallRoute", "route_recall"]


class RecallRoute(ContractModel):
    """서버가 evidence gap을 전문 Agent와 실행 상한으로 변환한 결과."""

    target_agent: Literal[AnalysisStageName.ADMET] = AnalysisStageName.ADMET
    max_tool_calls: Literal[1] = 1
    max_recall_depth: Literal[1] = 1


def route_recall(request: DecisionRecallRequest, *, current_depth: int) -> RecallRoute:
    """한 분석에서 재귀 recall을 막고 지원하는 공백만 실행 경계로 전달한다."""
    if current_depth != 0:
        raise ValueError("recall_depth_exceeded")
    if request.gap_kind is not EvidenceGapKind.CARDIAC_ION_CHANNEL:
        raise ValueError("unsupported_evidence_gap")
    return RecallRoute()
