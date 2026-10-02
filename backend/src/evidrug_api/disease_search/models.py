"""질환 검색 API의 요청과 응답 모델."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DiseaseSearchRequest(BaseModel):
    """사용자가 입력한 영어 질환명 또는 표현형."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=120)

    @model_validator(mode="after")
    def validate_english_query(self) -> Self:
        query = self.query.strip()
        contains_english_letter = any("a" <= char.lower() <= "z" for char in query)
        if not query.isascii() or not contains_english_letter:
            raise ValueError("query must be an English disease or phenotype name")
        self.query = query
        return self


class DiseaseCandidate(BaseModel):
    """Open Targets에 실제로 존재하는 질환 또는 표현형 후보."""

    id: str
    name: str
    description: str | None = None
    relevance_score: float


class DiseaseSearchResponse(BaseModel):
    """사용자가 한 후보를 명시적으로 확인하기 전의 검색 결과."""

    query: str
    candidates: list[DiseaseCandidate]
    requires_confirmation: Literal[True] = True
    source: Literal["open_targets"] = "open_targets"
