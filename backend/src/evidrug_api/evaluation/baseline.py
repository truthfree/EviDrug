"""원본 Agent 출력을 수정하지 않는 threshold baseline 입력 projection."""

from collections.abc import Callable
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from evidrug_api.admet.agent import AdmetAgentResult
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.analysis_jobs.repository import AnalysisRepository
from evidrug_api.analysis_jobs.result_models import (
    AdmetSummary,
    DtaSummary,
    PublicRun,
)
from evidrug_api.analysis_jobs.results import project_admet, project_dta, project_run
from evidrug_api.dta.agent import DtaAgentResult
from evidrug_api.execution_contracts.agent import AgentOutput
from evidrug_api.orchestration.tables import AgentRunRecord


class BaselineRun[ResultT: BaseModel](BaseModel):
    """최신 run의 원본 참조와 검증된 공개 관측을 한데 묶는다."""

    run_id: UUID | None
    attempt: int | None
    output_sha256: str | None
    status: str | None
    projection_status: Literal["available", "unavailable", "invalid"]
    result: ResultT | None
    error_code: str | None


class BaselineSnapshot(BaseModel):
    """정답이나 threshold를 포함하지 않는 평가 입력. 버전 변경 시 별도 계약으로 갱신."""

    schema_version: Literal["1"] = "1"
    analysis_id: UUID
    disease_id: str
    disease_name: str
    target_name: str | None
    canonical_smiles: str
    dta: BaselineRun[DtaSummary]
    admet: BaselineRun[AdmetSummary]


def _run[StoredT: BaseModel, PublicT: BaseModel](
    row: AgentRunRecord | None,
    output_type: type[AgentOutput[StoredT]],
    projector: Callable[[StoredT], PublicT],
) -> BaselineRun[PublicT]:
    public: PublicRun[PublicT] = project_run(row, output_type, projector)
    return BaselineRun[PublicT](
        run_id=public.run_id,
        attempt=row.attempt if row else None,
        output_sha256=row.output_sha256 if row else None,
        status=public.status,
        projection_status=public.projection_status,
        result=public.result,
        error_code=public.error_code,
    )


async def extract_baseline_snapshot(
    repository: AnalysisRepository, analysis_id: UUID
) -> BaselineSnapshot:
    """같은 분석의 DTA와 최초 ADMET baseline을 읽으며 provider나 LLM은 실행하지 않는다."""
    analysis = await repository.get(analysis_id)
    if analysis is None:
        raise LookupError("analysis_not_found")
    dta = await repository.get_latest_run(analysis_id, AnalysisStageName.DTA)
    admet = await repository.get_run_attempt(analysis_id, AnalysisStageName.ADMET, 1)
    return BaselineSnapshot(
        analysis_id=analysis_id,
        disease_id=analysis.disease_id,
        disease_name=analysis.disease_name,
        target_name=analysis.target_name,
        canonical_smiles=analysis.canonical_smiles,
        dta=_run(dta, AgentOutput[DtaAgentResult], project_dta),
        admet=_run(admet, AgentOutput[AdmetAgentResult], project_admet),
    )
