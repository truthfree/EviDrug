"""Open Targets GraphQL 질환 검색 클라이언트."""

from typing import Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from evidrug_api.disease_search.models import DiseaseCandidate

SEARCH_DISEASES_QUERY = """
query SearchDisease($query: String!, $size: Int!) {
  search(
    queryString: $query
    entityNames: ["disease"]
    page: {index: 0, size: $size}
  ) {
    hits {
      id
      name
      description
      score
    }
  }
}
"""


class OpenTargetsUnavailable(RuntimeError):
    """Open Targets가 정상적인 검색 결과를 제공하지 못했을 때 발생한다."""


class DiseaseSearcher(Protocol):
    """라우터가 외부 제공자의 세부 구현 없이 사용하는 검색 계약."""

    async def search(self, query: str, limit: int) -> list[DiseaseCandidate]: ...


class _SearchHit(BaseModel):
    id: str
    name: str
    description: str | None = None
    score: float


class _SearchResults(BaseModel):
    hits: list[_SearchHit] = Field(default_factory=list)


class _SearchData(BaseModel):
    search: _SearchResults


class _GraphQLError(BaseModel):
    message: str


class _GraphQLResponse(BaseModel):
    data: _SearchData | None = None
    errors: list[_GraphQLError] = Field(default_factory=list)


class OpenTargetsDiseaseSearcher:
    """공개 GraphQL search를 호출하고 질환 후보만 반환한다."""

    def __init__(self, http_client: httpx.AsyncClient, graphql_url: str) -> None:
        self._http_client = http_client
        self._graphql_url = graphql_url

    async def search(self, query: str, limit: int) -> list[DiseaseCandidate]:
        try:
            response = await self._http_client.post(
                self._graphql_url,
                json={
                    "query": SEARCH_DISEASES_QUERY,
                    "variables": {"query": query, "size": limit},
                },
            )
            response.raise_for_status()
            payload = _GraphQLResponse.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            raise OpenTargetsUnavailable("Open Targets search is unavailable") from error

        if payload.errors or payload.data is None:
            raise OpenTargetsUnavailable("Open Targets search is unavailable")

        return [
            DiseaseCandidate(
                id=hit.id,
                name=hit.name,
                description=hit.description,
                relevance_score=hit.score,
            )
            for hit in payload.data.search.hits
        ]
