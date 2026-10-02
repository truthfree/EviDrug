import asyncio
from typing import Literal, TypedDict, cast
from uuid import UUID

from celery import Celery

from evidrug_api.config import get_settings
from evidrug_api.ctoxpred2.deployment import verify_ctox_installation
from evidrug_api.orchestration.recover import recover_expired_analyses
from evidrug_api.orchestration.repository import OrchestrationInProgress
from evidrug_api.orchestration.runtime import run_analysis_once
from evidrug_api.tool_execution.recover import recover_expired_executions


class PingResult(TypedDict):
    status: Literal["pong"]


class AnalysisTaskReceipt(TypedDict):
    """worker가 실행하거나 기존 실행을 확인한 분석의 상태."""

    analysis_id: str
    status: Literal["completed", "partial_failure", "failed", "in_progress"]


AnalysisTaskStatus = Literal["completed", "partial_failure", "failed", "in_progress"]


settings = get_settings()
if settings.ctoxpred2_recall_enabled:
    if not settings.poc_models_enabled:
        raise RuntimeError("ctoxpred2_requires_poc_models")
    verify_ctox_installation()
celery_app = Celery("evidrug", broker=settings.redis_url)
celery_app.conf.update(
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    enable_utc=True,
    result_backend=None,
    task_ignore_result=True,
    task_serializer="json",
    timezone="UTC",
    beat_schedule={
        "recover-expired-tool-executions": {
            "task": "evidrug.tools.recover_expired",
            "schedule": 60.0,
        },
        "recover-expired-analysis-executions": {
            "task": "evidrug.analysis.recover_expired",
            "schedule": 60.0,
        },
    },
)


@celery_app.task(name="evidrug.tools.recover_expired")  # type: ignore[untyped-decorator]
def recover_tool_executions() -> int:
    """Beat 또는 운영자가 요청한 만료 회수를 실행한다. 모델은 재호출하지 않는다."""
    return asyncio.run(recover_expired_executions(settings.database_url))


@celery_app.task(name="evidrug.analysis.recover_expired")  # type: ignore[untyped-decorator]
def recover_analysis_executions() -> int:
    """만료된 분석 실행과 남은 단계를 실패 상태로 회수한다."""
    return asyncio.run(recover_expired_analyses(settings.database_url))


@celery_app.task(name="evidrug.system.ping")  # type: ignore[untyped-decorator]
def ping() -> PingResult:
    return {"status": "pong"}


@celery_app.task(name="evidrug.analysis.run")  # type: ignore[untyped-decorator]
def run_analysis(analysis_id: str) -> AnalysisTaskReceipt:
    """고정 DAG orchestration을 실행하고 polling 가능한 영속 상태를 반환한다."""

    validated_id = UUID(analysis_id)
    try:
        status = asyncio.run(
            run_analysis_once(
                settings.database_url,
                validated_id,
                lease_seconds=settings.orchestration_lease_seconds,
                settings=settings,
            )
        )
    except OrchestrationInProgress:
        return {"analysis_id": str(validated_id), "status": "in_progress"}
    return {
        "analysis_id": str(validated_id),
        "status": cast(AnalysisTaskStatus, status.value),
    }
