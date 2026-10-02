"""운영 설정으로 Target Hypothesis Agent 의존성을 조립한다."""

from dataclasses import dataclass

import httpx

from evidrug_api.config import Settings
from evidrug_api.openai_gateway import DaconOpenAIClient, build_dacon_openai_client
from evidrug_api.target_hypothesis.agent import TargetHypothesisAgent
from evidrug_api.target_hypothesis.policy import SmallMoleculePrioritizationPolicy
from evidrug_api.target_hypothesis.providers import (
    OpenTargetsCandidateProvider,
    PharosTargetProvider,
    UniProtProteinProvider,
)
from evidrug_api.target_hypothesis.reasoner import DaconTargetReasoner


@dataclass(frozen=True, slots=True)
class TargetHypothesisRuntime:
    """한 worker task가 소유하는 Agent와 외부 연결 수명주기."""

    agent: TargetHypothesisAgent
    http_client: httpx.AsyncClient
    openai_client: DaconOpenAIClient

    async def aclose(self) -> None:
        try:
            await self.openai_client.close()
        finally:
            await self.http_client.aclose()


def build_target_hypothesis_agent(
    settings: Settings,
) -> TargetHypothesisRuntime:
    """한 worker task가 소유하고 종료할 실제 provider client를 구성한다."""
    openai_client = build_dacon_openai_client(settings)
    http_client = httpx.AsyncClient(timeout=settings.target_lookup_timeout_seconds)
    agent = TargetHypothesisAgent(
        OpenTargetsCandidateProvider(http_client, settings.open_targets_graphql_url),
        UniProtProteinProvider(http_client, settings.uniprot_base_url),
        DaconTargetReasoner(openai_client),
        SmallMoleculePrioritizationPolicy(),
        candidate_limit=settings.target_candidate_limit,
        shortlist_limit=settings.target_shortlist_limit,
        pharos_provider=(
            PharosTargetProvider(http_client, settings.pharos_graphql_url)
            if settings.pharos_enabled
            else None
        ),
    )
    return TargetHypothesisRuntime(
        agent=agent,
        http_client=http_client,
        openai_client=openai_client,
    )
