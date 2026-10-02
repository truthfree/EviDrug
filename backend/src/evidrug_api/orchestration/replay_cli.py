"""DB 운영자가 명시적으로 실행하는 개발용 Agent replay CLI. 공개 API가 아니다."""

import argparse
import asyncio
import json
from typing import cast
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from evidrug_api.analysis_jobs.models import AnalysisStageName, AnalysisStatus
from evidrug_api.config import Settings
from evidrug_api.database import create_database_engine, create_database_session_factory
from evidrug_api.execution_contracts.agent import AgentOutput
from evidrug_api.execution_contracts.common import ComponentVersion
from evidrug_api.orchestration.continuation import (
    build_continuation_executor,
    continuation_report,
    output_parsers,
    run_continuation,
)
from evidrug_api.orchestration.replay import replay_report, run_replay
from evidrug_api.orchestration.replay_contracts import ReplayError, ReplayRequest
from evidrug_api.orchestration.replay_store import ReplayStore, digest
from evidrug_api.orchestration.repository import OrchestrationInProgress
from evidrug_api.target_hypothesis.agent import IMPLEMENTATION_VERSION
from evidrug_api.target_hypothesis.contracts import TargetHypothesisResult
from evidrug_api.target_hypothesis.reasoner import PROMPT_VERSION
from evidrug_api.target_hypothesis.runtime import build_target_hypothesis_agent


def parse_target_output(serialized: str) -> AgentOutput[BaseModel]:
    return cast(
        AgentOutput[BaseModel], AgentOutput[TargetHypothesisResult].model_validate_json(serialized)
    )


def runtime_versions(settings: Settings) -> tuple[ComponentVersion, ...]:
    """비밀 값 없이 현재 실행 설정을 고정한다. live 데이터 릴리스는 output에 남는다."""
    components = {
        "implementation": IMPLEMENTATION_VERSION,
        "target_prompt": PROMPT_VERSION,
        "target_policy": "small-molecule-v1",
        "causal_policy": "open-targets-v2",
        "language_model": settings.openai_model,
        "candidate_limit": str(settings.target_candidate_limit),
        "shortlist_limit": str(settings.target_shortlist_limit),
        "lookup_timeout_seconds": str(settings.target_lookup_timeout_seconds),
        "model_timeout_seconds": str(settings.openai_timeout_seconds),
        "openai_max_retries": "0",
        # URL이 credential/query를 포함할 수 있으므로 원문을 로그·manifest에 쓰지 않는다.
        "provider_endpoints_sha256": digest(
            json.dumps(
                [
                    settings.open_targets_graphql_url,
                    settings.uniprot_base_url,
                    settings.openai_base_url,
                ]
            )
        ),
    }
    return tuple(
        ComponentVersion(component=name, version=version) for name, version in components.items()
    )


def continuation_versions(settings: Settings) -> tuple[ComponentVersion, ...]:
    """새로 호출하는 두 단계의 구현과 설정을 명시적으로 고정한다."""
    from evidrug_api.decision.agent import INSTRUCTIONS
    from evidrug_api.dta.deeppurpose import (
        CNN_CNN_BINDINGDB_MODEL,
        MPNN_CNN_BINDINGDB_MODEL,
    )
    from evidrug_api.orchestration.reasoning import INSTRUCTIONS as SPECIALIST_INSTRUCTIONS

    return (
        ComponentVersion(component="continuation", version="dta-decision-v2"),
        ComponentVersion(component="dta_agent", version="dta-shortlist-agent-v4"),
        ComponentVersion(
            component="dta_model_cnn_cnn",
            version=digest(CNN_CNN_BINDINGDB_MODEL.model_dump_json()),
        ),
        ComponentVersion(
            component="dta_model_mpnn_cnn",
            version=digest(MPNN_CNN_BINDINGDB_MODEL.model_dump_json()),
        ),
        ComponentVersion(component="decision_agent", version="decision-agent-v6"),
        ComponentVersion(component="decision_prompt", version=digest(INSTRUCTIONS)),
        ComponentVersion(component="specialist_prompt", version=digest(SPECIALIST_INSTRUCTIONS)),
        ComponentVersion(component="language_model", version=settings.openai_model),
        ComponentVersion(component="model_timeout", version=str(settings.openai_timeout_seconds)),
        ComponentVersion(component="openai_max_retries", version="0"),
        ComponentVersion(component="endpoint", version=digest(settings.openai_base_url)),
    )


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="개발용 단일 Agent 실행. prepare/show는 외부 API를 호출하지 않습니다."
    )
    commands = command.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="원본을 검증하고 새 분석 ID의 실행 계획 저장")
    prepare.add_argument("--base-analysis-id", type=UUID, required=True)
    prepare.add_argument("--source-run-id", type=UUID, required=True)
    prepare.add_argument(
        "--agent", choices=[name.value for name in AnalysisStageName], required=True
    )
    prepare.add_argument(
        "--mode",
        choices=["live_agent", "reuse_output", "target_from_snapshot", "continue_dta_decision"],
        required=True,
    )
    prepare.add_argument("--upstream-run-id", type=UUID, action="append", default=[])
    prepare.add_argument(
        "--analysis-id", type=UUID, help="동일 prepare 재전달 시 사용할 새 분석 UUID"
    )
    run = commands.add_parser("run", help="저장 계획 실행. live_agent는 --allow-live 필요")
    run.add_argument("--analysis-id", type=UUID, required=True)
    run.add_argument("--allow-live", action="store_true")
    show = commands.add_parser("show", help="상태와 새 비용/원본 비용 조회")
    show.add_argument("--analysis-id", type=UUID, required=True)
    return command


async def execute(args: argparse.Namespace, settings: Settings) -> int:
    """Target 단독 replay와 명시적 DTA/Decision continuation만 허용한다."""
    if settings.environment == "production":
        raise ReplayError("replay_development_only")
    engine = create_database_engine(settings.database_url)
    store = ReplayStore(
        create_database_session_factory(engine),
        {**output_parsers(), AnalysisStageName.TARGET_HYPOTHESIS: parse_target_output},
    )
    try:
        if args.command == "prepare":
            request = ReplayRequest(
                base_analysis_id=args.base_analysis_id,
                source_run_id=args.source_run_id,
                agent_name=AnalysisStageName(args.agent),
                mode=args.mode,
                upstream_run_ids=tuple(args.upstream_run_id),
            )
            if (
                request.agent_name is not AnalysisStageName.TARGET_HYPOTHESIS
                and request.mode != "continue_dta_decision"
            ):
                raise ReplayError("replay_agent_not_configured")
            versions = (
                continuation_versions(settings)
                if request.mode == "continue_dta_decision"
                else runtime_versions(settings)
                if request.mode == "live_agent"
                else ()
            )
            manifest = await store.manifest(request, versions)
            analysis_id = await store.prepare(args.analysis_id or uuid4(), manifest)
            report = (
                await continuation_report(store, analysis_id)
                if request.mode == "continue_dta_decision"
                else await replay_report(store, analysis_id)
            )
            print(report.model_dump_json(indent=2))
            return 0
        if args.command == "show":
            plan = await store.load(args.analysis_id)
            report = (
                await continuation_report(store, args.analysis_id)
                if plan.request.mode == "continue_dta_decision"
                else await replay_report(store, args.analysis_id)
            )
            print(report.model_dump_json(indent=2))
            return 0
        manifest = await store.validate(args.analysis_id)
        if manifest.request.mode == "continue_dta_decision":
            if not args.allow_live:
                raise ReplayError("replay_live_confirmation_required")
            if not settings.poc_models_enabled:
                raise ReplayError("replay_poc_worker_required")
            versions = continuation_versions(settings)
            if manifest.runtime_versions != versions:
                raise ReplayError("replay_runtime_version_mismatch")
            if (
                settings.openai_api_key is None
                or not settings.openai_api_key.get_secret_value().strip()
            ):
                raise ReplayError("replay_live_executor_missing")
            executor = build_continuation_executor(store, settings)
            try:
                report = await run_continuation(
                    store,
                    args.analysis_id,
                    executor,
                    versions,
                    lease_seconds=settings.orchestration_lease_seconds,
                )
            finally:
                await executor.aclose()
            print(report.model_dump_json(indent=2))
            return 1 if report.status is AnalysisStatus.FAILED else 0
        if manifest.request.agent_name is not AnalysisStageName.TARGET_HYPOTHESIS:
            raise ReplayError("replay_agent_not_configured")
        if manifest.request.mode == "live_agent":
            if not args.allow_live:
                raise ReplayError("replay_live_confirmation_required")
            if manifest.runtime_versions != runtime_versions(settings):
                raise ReplayError("replay_runtime_version_mismatch")
            if (
                settings.openai_api_key is None
                or not settings.openai_api_key.get_secret_value().strip()
            ):
                raise ReplayError("replay_live_executor_missing")
            runtime = build_target_hypothesis_agent(settings)
            try:
                report = await run_replay(
                    store,
                    args.analysis_id,
                    live_executor=runtime.agent,
                    runtime_versions=runtime_versions(settings),
                    lease_seconds=settings.orchestration_lease_seconds,
                )
            finally:
                await runtime.aclose()
        else:
            report = await run_replay(store, args.analysis_id)
        print(report.model_dump_json(indent=2))
        return 1 if report.status is AnalysisStatus.FAILED else 0
    finally:
        await engine.dispose()


def main() -> None:
    """설정은 컨테이너의 환경변수만 읽으며 비밀정보와 결과 본문을 출력하지 않는다."""
    args = parser().parse_args()
    try:
        # BaseSettings의 런타임 인수이며 pydantic mypy 생성 signature에는 포함되지 않는다.
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        code = asyncio.run(execute(args, settings))
    except ReplayError as error:
        print(json.dumps({"error": error.code}))
        code = 2
    except OrchestrationInProgress:
        print(json.dumps({"error": "replay_in_progress"}))
        code = 2
    except SQLAlchemyError:
        print(
            json.dumps(
                {
                    "error": "replay_database_error",
                    "hint": "DB 연결 및 migration 0007 적용 여부를 확인하세요.",
                },
                ensure_ascii=False,
            )
        )
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
