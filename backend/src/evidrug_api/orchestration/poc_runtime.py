"""PoC worker의 고정 모델 경로와 전문 Agent 수명주기를 조립한다."""

import asyncio
from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evidrug_api.admet.adapter import AdmetToolAdapter
from evidrug_api.admet.agent import AdmetAgent
from evidrug_api.admet.provider import AdmetSubprocessProvider
from evidrug_api.admet.recall import AdmetRecallAgent
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.ctoxpred2.adapter import CtoxToolAdapter
from evidrug_api.ctoxpred2.deployment import verify_ctox_installation
from evidrug_api.ctoxpred2.provider import CtoxPred2Provider
from evidrug_api.decision.agent import DecisionAgent
from evidrug_api.dta.adapter import DtaToolAdapter
from evidrug_api.dta.agent import DtaAgent
from evidrug_api.dta.assay_providers import PubchemBioassayProvider
from evidrug_api.dta.deeppurpose import (
    CNN_CNN_BINDINGDB_MODEL,
    MPNN_CNN_BINDINGDB_MODEL,
    DeepPurposeProvider,
)
from evidrug_api.execution_contracts.agent import AgentInput
from evidrug_api.orchestration.contracts import AgentExecutionResult, AgentExecutor
from evidrug_api.orchestration.executor import RoutedAgentExecutor
from evidrug_api.orchestration.reasoning import DaconSpecialistReasoner
from evidrug_api.target_hypothesis.runtime import TargetHypothesisRuntime
from evidrug_api.tool_admission.bindings import (
    admet_binding,
    ctoxpred2_binding,
    dta_assay_binding,
    dta_binding,
)
from evidrug_api.tool_admission.registry import ToolBinding


class ExclusiveModelAgent:
    """한 worker task 내 ADMET/DTA 모델 메모리가 동시에 상주하지 않게 한다."""

    def __init__(
        self,
        agent: AgentExecutor,
        lock: asyncio.Lock,
        close: Callable[[], Awaitable[None]],
    ) -> None:
        self.agent, self.lock, self.close = agent, lock, close

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        """DTA 후보 간 warm 재사용은 유지하고 stage 종료/취소 시 모델을 회수한다."""
        async with self.lock:
            try:
                return await self.agent.execute(agent_input)
            finally:
                await self.close()


def build_poc_executor(
    factory: async_sessionmaker[AsyncSession],
    target: TargetHypothesisRuntime,
    *,
    enable_ctoxpred2_recall: bool = False,
    enable_dta_assay_providers: bool = False,
) -> RoutedAgentExecutor:
    """LLM/HTTP 입력이 바꿀 수 없는 서버 고정 argv로 provider를 만든다."""
    admet = AdmetSubprocessProvider(("/opt/admet/.venv/bin/python", "/opt/admet/runtime.py"))
    dta_cnn = DeepPurposeProvider(
        (
            "/opt/dta/.venv/bin/python",
            "/opt/dta/runtime.py",
            "--model",
            CNN_CNN_BINDINGDB_MODEL.model_id,
        ),
        CNN_CNN_BINDINGDB_MODEL,
    )
    dta_mpnn = DeepPurposeProvider(
        (
            "/opt/dta/.venv/bin/python",
            "/opt/dta/runtime.py",
            "--model",
            MPNN_CNN_BINDINGDB_MODEL.model_id,
        ),
        MPNN_CNN_BINDINGDB_MODEL,
    )

    async def close_dta() -> None:
        await dta_cnn.aclose()
        await dta_mpnn.aclose()

    reasoner = DaconSpecialistReasoner(target.openai_client)
    assay_client: httpx.AsyncClient | None = None
    assay_bindings: tuple[ToolBinding, ...] = ()
    if enable_dta_assay_providers:
        assay_client = httpx.AsyncClient(
            headers={"User-Agent": "EviDrug/0.1 research-assay-evidence"},
            follow_redirects=True,
            timeout=None,
        )
        assay_bindings = tuple(
            dta_assay_binding(provider) for provider in (PubchemBioassayProvider(assay_client),)
        )
    lock = asyncio.Lock()
    admet_tool = admet_binding(AdmetToolAdapter(admet))
    if enable_ctoxpred2_recall:
        verify_ctox_installation()
    ctox = (
        CtoxPred2Provider(
            (
                "/opt/ctoxpred2/.venv/bin/python",
                "/opt/ctoxpred2/runtime.py",
                "--artifact-root",
                "/opt/ctoxpred2/artifacts",
            )
        )
        if enable_ctoxpred2_recall
        else None
    )
    recall_agent = (
        AdmetRecallAgent(factory, admet_tool, ctoxpred2_binding(CtoxToolAdapter(ctox)))
        if ctox is not None
        else None
    )
    return RoutedAgentExecutor(
        {
            AnalysisStageName.TARGET_HYPOTHESIS: target.agent,
            AnalysisStageName.ADMET: ExclusiveModelAgent(
                AdmetAgent(factory, admet_tool, reasoner, recall_agent=recall_agent),
                lock,
                admet.aclose,
            ),
            AnalysisStageName.DTA: ExclusiveModelAgent(
                DtaAgent(
                    factory,
                    (
                        dta_binding(DtaToolAdapter(dta_cnn)),
                        dta_binding(
                            DtaToolAdapter(dta_mpnn),
                            tool_id="dta_mpnn_cnn_bindingdb",
                        ),
                    ),
                    reasoner,
                    assay_bindings=assay_bindings,
                ),
                lock,
                close_dta,
            ),
            AnalysisStageName.DECISION: DecisionAgent(factory, target.openai_client),
        },
        close_callbacks=(
            target.aclose,
            admet.aclose,
            close_dta,
            *((assay_client.aclose,) if assay_client is not None else ()),
            *((ctox.aclose,) if ctox is not None else ()),
        ),
    )
