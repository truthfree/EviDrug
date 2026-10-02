"""질환 검색 라우터의 교체 가능한 의존성."""

from typing import cast

from fastapi import Request

from evidrug_api.disease_search.client import DiseaseSearcher


def get_disease_searcher(request: Request) -> DiseaseSearcher:
    """애플리케이션 시작 시 구성한 질환 검색기를 반환한다."""

    return cast(DiseaseSearcher, request.app.state.disease_searcher)
