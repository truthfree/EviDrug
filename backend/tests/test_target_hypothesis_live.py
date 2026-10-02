"""명시적으로 활성화할 때만 실행하는 Open Targets provider smoke test."""

import os

import httpx
import pytest

from evidrug_api.target_hypothesis.providers import OpenTargetsCandidateProvider

RUN_LIVE = os.getenv("EVIDRUG_RUN_LIVE_PROVIDER_TESTS") == "1"


@pytest.mark.skipif(not RUN_LIVE, reason="live provider tests are opt-in")
@pytest.mark.asyncio
@pytest.mark.parametrize("target_name", ["PIK3CA", "BRCA2", "CDK4"])
async def test_open_targets_breast_cancer_causal_evidence_smoke(
    target_name: str,
) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        batch = await OpenTargetsCandidateProvider(
            client,
            "https://api.platform.opentargets.org/api/v4/graphql",
        ).find_candidates(
            disease_id="MONDO_0007254",
            target_name=target_name,
            limit=5,
        )

    assert batch.disease_name == "breast cancer"
    assert batch.candidates[0].approved_symbol == target_name
    assert batch.candidates[0].causal_evidence
    assert batch.source_version.startswith("api-")
