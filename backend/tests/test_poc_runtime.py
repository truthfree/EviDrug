"""worker의 실제 조립 진입점도 유료 호출 없이 네 단계를 연결하는지 검증한다."""

import json
from typing import cast

import httpx
import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_dta_agent import CandidateProvider
from test_orchestration import orchestration_database as database_fixture
from test_poc_agents import DecisionClient, FullAdmetProvider
from test_target_hypothesis import create_analysis, target_agent

from evidrug_api.config import Settings
from evidrug_api.dta.deeppurpose import MPNN_CNN_BINDINGDB_MODEL
from evidrug_api.openai_gateway.client import DaconOpenAIClient
from evidrug_api.openai_gateway.models import DaconQuota, GeneratedText, ResponseUsage
from evidrug_api.orchestration.runtime import run_analysis_once
from evidrug_api.target_hypothesis.runtime import TargetHypothesisRuntime

orchestration_database = database_fixture


class RuntimeClient(DecisionClient):
    closed = False

    async def generate_text(
        self,
        prompt: str,
        *,
        instructions: str,
        max_output_tokens: int,
        output_schema: type[BaseModel] | None = None,
    ) -> GeneratedText:
        payload = json.loads(prompt)
        if "required_citation_prefixes" in payload:
            return await super().generate_text(
                prompt,
                instructions=instructions,
                max_output_tokens=max_output_tokens,
                output_schema=output_schema,
            )
        assert max_output_tokens == 4096
        self.calls.append(prompt)
        evidence = payload["evidence"]
        assert payload["allowed_evidence_ids"] == list(evidence)
        assert payload["observation_evidence_ids"] == [
            key for key in evidence if key != "model_context"
        ]
        assert "Copy used_evidence_ids exactly from allowed_evidence_ids" in instructions
        return GeneratedText(
            "synthetic",
            "gpt-5.6-luna",
            json.dumps(
                {
                    "summary": "합성 관측 해석",
                    "used_evidence_ids": [next(iter(evidence))],
                    "limitations": ["실험 근거 아님"],
                }
            ),
            ResponseUsage(10, 10, 20),
            DaconQuota(None, None, None, None),
        )

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_worker_entrypoint_selects_poc_handlers_and_closes_resources(
    orchestration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    factory = orchestration_database
    analysis_id = await create_analysis(factory)
    client = RuntimeClient()
    admet, dta, mpnn = FullAdmetProvider(), CandidateProvider(), CandidateProvider()
    mpnn.model = MPNN_CNN_BINDINGDB_MODEL
    closed: list[str] = []

    async def close_admet() -> None:
        closed.append("admet")

    async def close_dta() -> None:
        closed.append("dta")

    # provider는 fake로 교체하지만 routing/실제 Agent/admission/SQL 경로는 교체하지 않는다.
    monkeypatch.setattr(admet, "aclose", close_admet, raising=False)
    monkeypatch.setattr(dta, "aclose", close_dta, raising=False)
    monkeypatch.setattr(mpnn, "aclose", close_dta, raising=False)
    monkeypatch.setattr(
        "evidrug_api.orchestration.poc_runtime.AdmetSubprocessProvider", lambda command: admet
    )
    monkeypatch.setattr(
        "evidrug_api.orchestration.poc_runtime.DeepPurposeProvider",
        lambda command, model: mpnn if model == MPNN_CNN_BINDINGDB_MODEL else dta,
    )
    http = httpx.AsyncClient()
    target = TargetHypothesisRuntime(target_agent(), http, cast(DaconOpenAIClient, client))
    monkeypatch.setattr(
        "evidrug_api.orchestration.runtime.build_target_hypothesis_agent", lambda settings: target
    )
    settings = Settings(  # type: ignore[call-arg]  # BaseSettings의 dotenv 비활성화 인수
        _env_file=None, openai_api_key=SecretStr("test-key"), poc_models_enabled=enabled
    )
    engine = cast(AsyncEngine, factory.kw["bind"])
    status = await run_analysis_once(
        str(engine.url), analysis_id, lease_seconds=1200, settings=settings
    )
    assert status == ("completed" if enabled else "failed")
    assert len(client.calls) == (4 if enabled else 0)
    if enabled:
        tasks = [json.loads(call).get("task") for call in client.calls[:3]]
        assert set(tasks) == {"ADME", "TOXICITY", "DTA"}
        assert tasks.index("ADME") < tasks.index("TOXICITY")
    assert admet.calls == (1 if enabled else 0)
    assert len(dta.calls) == (1 if enabled else 0)
    assert len(mpnn.calls) == (1 if enabled else 0)
    assert client.closed and http.is_closed
    assert set(closed) == ({"admet", "dta"} if enabled else set())
