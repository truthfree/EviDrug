"""ADMET 원본 실행 한 번에서 ADME와 독성 해석을 독립적으로 생성한다."""

from datetime import UTC, datetime
from typing import cast

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evidrug_api.admet.context import AdmetContext, AdmetContextBuilder, AdmetContextQuery
from evidrug_api.admet.contracts import AdmetToolArguments, AdmetToolResult
from evidrug_api.admet.toxicity import ToxicityAxes, calculate_toxicity_axes
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.execution_contracts.agent import (
    AgentInput,
    AgentOutput,
    AgentOutputStatus,
    AgentWarning,
)
from evidrug_api.execution_contracts.common import (
    ComponentVersion,
    ContractModel,
    ExecutionError,
    ExecutionMetadata,
    ExecutionUsage,
    TokenUsage,
)
from evidrug_api.orchestration.contracts import AgentExecutionResult, AgentExecutor
from evidrug_api.orchestration.fixed_tools import FixedToolRun
from evidrug_api.orchestration.reasoning import (
    Interpretation,
    ReasoningOutcome,
    SpecialistReasoner,
)
from evidrug_api.tool_admission.registry import ToolBinding

TOXICITY_ENDPOINTS = (
    "AMES",
    "hERG",
    "DILI",
    "LD50_Zhu",
)
ADME_ENDPOINTS = (
    "Solubility_AqSolDB",
    "Caco2_Wang",
    "HIA_Hou",
    "Bioavailability_Ma",
    "logP",
    "tpsa",
    "molecular_weight",
    "PPBR_AZ",
    "Clearance_Hepatocyte_AZ",
    "Clearance_Microsome_AZ",
    "CYP3A4_Veith",
    "CYP2D6_Veith",
)
POC_ENDPOINTS = TOXICITY_ENDPOINTS + ADME_ENDPOINTS
SELECTION_VERSION = "poc-admet-domain-endpoints-v2"


class AdmetDomainInterpretation(ContractModel):
    """영역별 선택 근거와 해석 실패를 다른 영역과 섞지 않는다."""

    endpoint_ids: tuple[str, ...]
    interpretation: Interpretation | None
    error_code: str | None = None
    token_usage: TokenUsage | None = None
    external_requests: int = 0


class AdmetAgentResult(ContractModel):
    context: AdmetContext
    interpretation: Interpretation | None
    adme: AdmetDomainInterpretation | None = None
    toxicity: AdmetDomainInterpretation | None = None
    selection_version: str = "poc-admet-endpoints-v1"
    missing_endpoints: tuple[str, ...] = ()
    toxicity_axes: ToxicityAxes | None = None


class AdmetAgent:
    """원본 전체는 SQL에 저장하고 선택된 endpoint만 LLM에 전달한다."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        binding: ToolBinding,
        reasoner: SpecialistReasoner,
        recall_agent: AgentExecutor | None = None,
    ) -> None:
        self.factory, self.binding, self.reasoner = factory, binding, reasoner
        self.recall_agent = recall_agent

    async def execute(self, agent_input: AgentInput) -> AgentExecutionResult:
        """예측 또는 해석 실패를 성공으로 바꾸지 않고 사용량을 기록한다."""
        if agent_input.recall_request is not None:
            if self.recall_agent is None:
                raise ValueError("admet_recall_not_configured")
            return await self.recall_agent.execute(agent_input)
        if agent_input.agent_name != AnalysisStageName.ADMET or agent_input.upstream_outputs:
            raise ValueError("admet_input_mismatch")
        started = datetime.now(UTC)
        tools = FixedToolRun(self.factory, self.binding, agent_input)
        await tools.start()
        observed = await tools.execute(
            "admet",
            AdmetToolArguments(
                canonical_smiles=agent_input.case_input.canonical_smiles,
            ),
            "분자의 ADMET 모델 관측 생성",
        )
        result = None
        status = AgentOutputStatus.FAILED
        error = observed.error
        usage = ExecutionUsage(tool_calls=observed.execution_metadata.usage.tool_calls)
        components: tuple[ComponentVersion, ...] = (
            ComponentVersion(component="admet_selection", version=SELECTION_VERSION),
            ComponentVersion(component="adme_prompt", version="adme-interpretation-v4"),
            ComponentVersion(component="toxicity_prompt", version="toxicity-interpretation-v3"),
        )
        warnings: tuple[AgentWarning, ...] = ()
        if observed.result is not None:
            predicted = AdmetToolResult.model_validate(observed.result.model_dump())
            available = {item.endpoint_id for item in predicted.predictions}
            selected = tuple(item for item in POC_ENDPOINTS if item in available)
            if not selected:
                error = ExecutionError(
                    code="admet_context_empty",
                    message="선택할 ADMET endpoint가 없습니다.",
                    retryable=False,
                )
            else:
                async with self.factory() as session:
                    context = await AdmetContextBuilder(session).build(
                        analysis_id=agent_input.analysis_id,
                        query=AdmetContextQuery(
                            source_tool_call_id=observed.tool_call_id, endpoint_ids=selected
                        ),
                    )
                evidence: dict[str, object] = {
                    row.endpoint_id: dict(zip(context.columns, row, strict=True))
                    for row in context.rows
                }
                model_context = {
                    "tool_version": context.tool_version,
                    "reference_population": context.reference_population,
                    "limitations": context.limitations,
                    "interpretation_note": context.interpretation_note,
                    "selection_is_subset_of_catalog": context.is_partial,
                    "selection_note": "Selected subset is not execution failure.",
                    "missing_requested_endpoints": [
                        item for item in POC_ENDPOINTS if item not in available
                    ],
                }
                domains: dict[str, AdmetDomainInterpretation] = {}
                toxicity_axes = calculate_toxicity_axes(context)
                domain_usages: list[TokenUsage | None] = []
                external_requests = 0
                domain_warnings: list[AgentWarning] = []
                remaining_limit = agent_input.execution_limits.max_total_tokens
                for task, endpoint_ids in (
                    ("ADME", ADME_ENDPOINTS),
                    ("TOXICITY", TOXICITY_ENDPOINTS),
                ):
                    selected_ids = tuple(item for item in endpoint_ids if item in evidence)
                    domain_evidence = {item: evidence[item] for item in selected_ids}
                    domain_evidence["model_context"] = model_context
                    if task == "TOXICITY":
                        domain_evidence["model_context"] = {
                            **model_context,
                            "toxicity_axes": toxicity_axes.model_dump(mode="json"),
                        }
                    if not selected_ids:
                        interpreted = ReasoningOutcome(
                            None, error_code="admet_domain_context_empty"
                        )
                    elif remaining_limit is not None and (
                        remaining_limit <= agent_input.execution_limits.reserved_finalization_tokens
                    ):
                        interpreted = ReasoningOutcome(
                            None, error_code="reasoning_input_budget_exceeded"
                        )
                    else:
                        limits = agent_input.execution_limits
                        if remaining_limit is not None:
                            limits = limits.model_copy(update={"max_total_tokens": remaining_limit})
                        interpreted = await self.reasoner.interpret(task, domain_evidence, limits)
                    domain_error = interpreted.error_code or (
                        "reasoning_invalid_output" if interpreted.interpretation is None else None
                    )
                    domains[task] = AdmetDomainInterpretation(
                        endpoint_ids=selected_ids,
                        interpretation=interpreted.interpretation,
                        error_code=domain_error,
                        token_usage=interpreted.usage,
                        external_requests=interpreted.external_requests,
                    )
                    domain_usages.append(interpreted.usage)
                    external_requests += interpreted.external_requests
                    components += interpreted.components
                    if remaining_limit is not None and interpreted.external_requests:
                        remaining_limit = (
                            max(0, remaining_limit - interpreted.usage.total_tokens)
                            if interpreted.usage is not None
                            else 0
                        )
                    if interpreted.diagnostic_code:
                        domain_warnings.append(
                            AgentWarning(
                                code=interpreted.diagnostic_code,
                                message=f"{task}: {interpreted.diagnostic_message}",
                            )
                        )
                    if interpreted.json_fence_removed:
                        domain_warnings.append(
                            AgentWarning(
                                code="reasoning_json_fence_removed",
                                message=(
                                    f"{task}: 단일 JSON 코드블록 포장을 제거한 뒤 "
                                    "내용·근거 ID 검증을 통과했습니다."
                                ),
                            )
                        )
                warnings = tuple(domain_warnings)
                missing = tuple(item for item in POC_ENDPOINTS if item not in available)
                summaries = tuple(
                    item.interpretation
                    for item in domains.values()
                    if item.interpretation is not None
                )
                combined = (
                    Interpretation(
                        # 구형 단일 필드에서도 한 영역이 다른 영역을 잘라내지 못하게 한다.
                        summary=" ".join(item.summary[:880] for item in summaries),
                        used_evidence_ids=tuple(
                            dict.fromkeys(
                                evidence_id
                                for item in summaries
                                for evidence_id in item.used_evidence_ids
                            )
                        )[:20],
                        limitations=tuple(
                            dict.fromkeys(
                                limitation for item in summaries for limitation in item.limitations
                            )
                        )[:8],
                    )
                    if summaries
                    else None
                )
                result = AdmetAgentResult(
                    toxicity_axes=toxicity_axes,
                    context=context,
                    interpretation=combined,
                    adme=domains["ADME"],
                    toxicity=domains["TOXICITY"],
                    selection_version=SELECTION_VERSION,
                    missing_endpoints=missing,
                )
                error_code = next(
                    (item.error_code for item in domains.values() if item.error_code), None
                ) or ("admet_endpoints_missing" if missing else None)
                status = (
                    AgentOutputStatus.PARTIAL_FAILURE if error_code else AgentOutputStatus.COMPLETED
                )
                error = (
                    ExecutionError(
                        code=error_code,
                        message="ADMET 해석 또는 선택 근거가 일부 부족합니다.",
                        retryable=False,
                    )
                    if error_code
                    else None
                )
                usage = ExecutionUsage(
                    token_usage=(
                        TokenUsage(
                            input_tokens=sum(item.input_tokens for item in domain_usages if item),
                            output_tokens=sum(item.output_tokens for item in domain_usages if item),
                            total_tokens=sum(item.total_tokens for item in domain_usages if item),
                        )
                        if domain_usages and all(item is not None for item in domain_usages)
                        else None
                    ),
                    tool_calls=usage.tool_calls,
                    external_requests=external_requests,
                )
        finished = datetime.now(UTC)
        output = AgentOutput[AdmetAgentResult](
            analysis_id=agent_input.analysis_id,
            run_id=agent_input.run_id,
            agent_name=agent_input.agent_name,
            status=status,
            result=result,
            error=error,
            warnings=warnings,
            execution_metadata=ExecutionMetadata(
                started_at=started,
                finished_at=finished,
                duration_ms=int((finished - started).total_seconds() * 1000),
                implementation_version="admet-agent-v3",
                components=components,
                usage=usage,
            ),
        )
        return AgentExecutionResult(cast(AgentOutput[BaseModel], output))
