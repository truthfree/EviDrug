"""Celery와 운영 회수 task가 사용하는 독립 database 진입점."""

from uuid import UUID

from evidrug_api.analysis_jobs.models import AnalysisStageName, AnalysisStatus
from evidrug_api.config import Settings, get_settings
from evidrug_api.database import create_database_engine, create_database_session_factory
from evidrug_api.execution_contracts.common import ExecutionLimits
from evidrug_api.orchestration.contracts import (
    AgentExecutor,
    ExecutionProfile,
    UnavailableAgentExecutor,
)
from evidrug_api.orchestration.executor import RoutedAgentExecutor
from evidrug_api.orchestration.poc_runtime import build_poc_executor
from evidrug_api.orchestration.service import AnalysisOrchestrator
from evidrug_api.target_hypothesis import build_target_hypothesis_agent


async def run_analysis_once(
    database_url: str,
    analysis_id: UUID,
    *,
    lease_seconds: int,
    executor: AgentExecutor | None = None,
    settings: Settings | None = None,
) -> AnalysisStatus:
    """독립 engine에서 한 분석을 실행하고 모든 연결을 정리한다."""
    engine = create_database_engine(database_url)
    runtime_executor: RoutedAgentExecutor | None = None
    profile: ExecutionProfile | None = None
    try:
        selected_executor = executor
        if selected_executor is None:
            runtime_settings = settings or get_settings()
            if (
                runtime_settings.ctoxpred2_recall_enabled
                and not runtime_settings.poc_models_enabled
            ):
                raise ValueError("CToxPred2 recall requires the PoC model runtime")
            secret = runtime_settings.openai_api_key
            if secret is None or not secret.get_secret_value().strip():
                selected_executor = UnavailableAgentExecutor()
            else:
                target_runtime = build_target_hypothesis_agent(runtime_settings)
                if runtime_settings.poc_models_enabled:
                    runtime_executor = build_poc_executor(
                        create_database_session_factory(engine),
                        target_runtime,
                        enable_ctoxpred2_recall=runtime_settings.ctoxpred2_recall_enabled,
                        enable_dta_assay_providers=(runtime_settings.dta_assay_providers_enabled),
                    )
                    profile = ExecutionProfile(
                        profile_id=(
                            "breast-cancer-poc-recall-v1"
                            if runtime_settings.ctoxpred2_recall_enabled
                            else "breast-cancer-poc-v1"
                        ),
                        policy_version=(
                            "fixed-dag-poc-recall-v1"
                            if runtime_settings.ctoxpred2_recall_enabled
                            else "fixed-dag-poc-v1"
                        ),
                        stage_limits=ExecutionLimits(
                            timeout_seconds=300,
                            # assay 활성화 시 후보마다 예측 2회, 기본 DB 2회,
                            # 조건부 PubChem 최대 1회를 허용한다.
                            max_tool_calls=runtime_settings.target_shortlist_limit
                            * (5 if runtime_settings.dta_assay_providers_enabled else 2),
                            max_recall_depth=(
                                1 if runtime_settings.ctoxpred2_recall_enabled else 0
                            ),
                        ),
                    )
                else:
                    runtime_executor = RoutedAgentExecutor(
                        {AnalysisStageName.TARGET_HYPOTHESIS: target_runtime.agent},
                        close_callbacks=(target_runtime.aclose,),
                    )
                selected_executor = runtime_executor
        orchestrator = AnalysisOrchestrator(
            create_database_session_factory(engine),
            selected_executor,
            lease_seconds=lease_seconds,
            profile=profile,
        )
        return await orchestrator.run(analysis_id)
    finally:
        if runtime_executor is not None:
            await runtime_executor.aclose()
        await engine.dispose()
