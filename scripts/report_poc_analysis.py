"""기존 worker에서 분석 한 건의 상태·버전·비용만 읽어 출력한다."""

import asyncio
import json
import os
import re
import sys
from uuid import UUID

from evidrug_api.admet.tables import AdmetPredictionRecord, AdmetToolExecutionRecord
from evidrug_api.analysis_jobs.tables import AnalysisRecord, AnalysisStageRecord
from evidrug_api.database import create_database_engine, create_database_session_factory
from evidrug_api.dta.tables import DtaExecutionRecord, DtaObservationRecord
from evidrug_api.orchestration.tables import AgentRunRecord, ExecutionTraceRecord
from evidrug_api.tool_execution.tables import ToolExecutionRecord
from sqlalchemy import func, select


def run_summary(row: AgentRunRecord) -> dict[str, object]:
    """SMILES, 서열, 프롬프트, 해석 원문과 입력 hash는 출력하지 않는다."""
    output = json.loads(row.output_json) if row.output_json else {}
    metadata = output.get("execution_metadata") or {}
    usage = metadata.get("usage") or {}
    warnings = output.get("warnings") or []
    citation_counts: list[dict[str, int]] = []
    for warning in warnings:
        message = warning.get("message") or ""
        unknown = re.search(r"허용 근거 ID (\d+)개, 미허용 인용 ID (\d+)개", message)
        missing = re.search(r"허용 관측 ID (\d+)개, 인용된 관측 ID 0개", message)
        if unknown:
            citation_counts.append(
                {
                    "allowed_id_count": int(unknown[1]),
                    "unknown_id_count": int(unknown[2]),
                }
            )
        elif missing:
            citation_counts.append({"observation_id_count": int(missing[1])})
    return {
        "agent": str(row.agent_name),
        "run_id": str(row.run_id),
        "status": row.status,
        "error_code": row.error_code,
        "implementation_version": metadata.get("implementation_version"),
        "components": [
            component
            for component in metadata.get("components", [])
            if component.get("component")
            in {"specialist_prompt", "language_model", "target_prompt"}
        ],
        "token_usage": usage.get("token_usage"),
        "tool_calls": usage.get("tool_calls"),
        "external_requests": usage.get("external_requests"),
        "warning_codes": [
            code
            for item in warnings
            if isinstance(code := item.get("code"), str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,119}", code)
        ],
        "citation_counts": citation_counts,
        "has_specialist_interpretation": (
            bool((output.get("result") or {}).get("interpretation"))
            if str(row.agent_name) in {"admet", "dta"}
            else None
        ),
    }


async def report(analysis_id: UUID) -> dict[str, object]:
    database_url = os.environ.get("EVIDRUG_DATABASE_URL")
    if not database_url:
        raise RuntimeError("database_configuration_missing")
    engine = create_database_engine(database_url)
    try:
        factory = create_database_session_factory(engine)
        async with factory() as session:
            analysis = await session.get(AnalysisRecord, analysis_id)
            if analysis is None:
                raise RuntimeError("analysis_not_found")
            stages = list(
                await session.scalars(
                    select(AnalysisStageRecord)
                    .where(AnalysisStageRecord.analysis_id == analysis_id)
                    .order_by(AnalysisStageRecord.position)
                )
            )
            runs = list(
                await session.scalars(
                    select(AgentRunRecord)
                    .where(AgentRunRecord.analysis_id == analysis_id)
                    .order_by(AgentRunRecord.started_at)
                )
            )
            dta_observations = (
                await session.execute(
                    select(
                        DtaExecutionRecord.run_id,
                        DtaExecutionRecord.status,
                        DtaObservationRecord.score_type,
                        DtaObservationRecord.value,
                        DtaObservationRecord.unit,
                    )
                    .join(
                        DtaObservationRecord,
                        DtaObservationRecord.tool_call_id
                        == DtaExecutionRecord.tool_call_id,
                    )
                    .where(DtaExecutionRecord.analysis_id == analysis_id)
                    .order_by(DtaExecutionRecord.run_id, DtaObservationRecord.position)
                )
            ).all()
            admet_prediction_count = await session.scalar(
                select(func.count())
                .select_from(AdmetPredictionRecord)
                .join(
                    AdmetToolExecutionRecord,
                    AdmetPredictionRecord.tool_call_id
                    == AdmetToolExecutionRecord.tool_call_id,
                )
                .where(AdmetToolExecutionRecord.analysis_id == analysis_id)
            )
            tool_executions = (
                await session.execute(
                    select(ToolExecutionRecord.tool_id, ToolExecutionRecord.status)
                    .where(ToolExecutionRecord.analysis_id == analysis_id)
                    .order_by(ToolExecutionRecord.started_at)
                )
            ).all()
            trace = (
                await session.execute(
                    select(
                        ExecutionTraceRecord.event_type,
                        ExecutionTraceRecord.reason_code,
                    )
                    .where(ExecutionTraceRecord.analysis_id == analysis_id)
                    .order_by(ExecutionTraceRecord.created_at)
                )
            ).all()
            return {
                "analysis_id": str(analysis_id),
                "status": analysis.status.value,
                "error_code": analysis.error_code,
                "disease_id": analysis.disease_id,
                "target_name": analysis.target_name,
                "stages": [
                    {"name": stage.name.value, "status": stage.status.value}
                    for stage in stages
                ],
                "agent_runs": [run_summary(run) for run in runs],
                "admet_prediction_count": admet_prediction_count,
                "dta_observations": [
                    {
                        "run_id": str(run_id),
                        "execution_status": status,
                        "score_type": score_type,
                        "value": value,
                        "unit": unit,
                    }
                    for run_id, status, score_type, value, unit in dta_observations
                ],
                "tool_executions": [
                    {"tool_id": tool_id, "status": status}
                    for tool_id, status in tool_executions
                ],
                "trace": [
                    {"event_type": event_type, "reason_code": reason_code}
                    for event_type, reason_code in trace
                ],
            }
    finally:
        await engine.dispose()


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: report_poc_analysis.py ANALYSIS_ID", file=sys.stderr)
        return 2
    try:
        analysis_id = UUID(sys.argv[1])
        result = asyncio.run(report(analysis_id))
    except (ValueError, RuntimeError) as error:
        print(str(error).splitlines()[0], file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - DB 오류 원문에 연결 문자열이 포함될 수 있다.
        print("analysis_report_unavailable", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
