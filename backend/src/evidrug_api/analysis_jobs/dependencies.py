"""분석 작업 router가 애플리케이션 의존성을 가져오는 함수."""

from typing import cast

from fastapi import Request

from evidrug_api.analysis_jobs.dispatcher import AnalysisDispatcher


def get_analysis_dispatcher(request: Request) -> AnalysisDispatcher:
    """앱 구성 시 고정한 queue adapter를 반환한다."""

    return cast(AnalysisDispatcher, request.app.state.analysis_dispatcher)
