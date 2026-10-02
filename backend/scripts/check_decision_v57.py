"""첨부 개발 사례만 각 3회 호출한다. 기본은 비용 없는 입력 점검이다."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from decision_v57_support import case_fixture  # noqa: E402

from evidrug_api.config import Settings  # noqa: E402
from evidrug_api.decision.agent import (  # noqa: E402
    DECISION_MAX_OUTPUT_TOKENS,
    DECISION_PROMPT_VERSION,
    INSTRUCTIONS,
    DecisionAssessmentGeneration,
    InvalidDecisionOutput,
    normalize_reader_numbers,
    remove_inline_evidence_markers,
    validate_assessment,
)
from evidrug_api.decision.policy import calculate_metadata  # noqa: E402
from evidrug_api.decision.projection import decision_payload_json  # noqa: E402
from evidrug_api.decision.validation import validate_policy  # noqa: E402
from evidrug_api.openai_gateway.client import build_dacon_openai_client  # noqa: E402
from evidrug_api.openai_gateway.diagnostics import model_call_diagnostic  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="실제 유료 모델을 총 6회 호출")
    parser.add_argument("--report", type=Path, required=True, help="새 JSON 보고서 경로")
    args = parser.parse_args()
    if args.report.exists():
        parser.error("기존 보고서는 덮어쓰지 않습니다. 새 경로를 지정하세요.")
    report: dict[str, object] = {
        "live": args.live,
        "input_source": "attached_development_cases_reconstructed_to_current_projection",
        "evaluation_cases_used": 0,
        "prompt_version": DECISION_PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(INSTRUCTIONS.encode()).hexdigest(),
        "max_output_tokens": DECISION_MAX_OUTPUT_TOKENS,
        "code_commit": os.environ.get("EVIDRUG_VALIDATION_CODE_COMMIT", "unrecorded"),
        "execution_environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "mode": "isolated_worker_subprocess",
        },
        "started_at_utc": datetime.now(UTC).isoformat(),
        "automatic_retries": 0,
        "maximum_paid_calls": 6,
    }
    # 보호된 dotenv 파일을 읽지 않는다. 환경 변수 또는 개발 worker의 주입만 사용한다.
    Settings.model_config["env_file"] = None
    settings = Settings(openai_max_retries=0)
    if args.live and settings.openai_api_key is None:
        parser.error("실행 환경에 EVIDRUG_OPENAI_API_KEY가 주입되어 있어야 합니다.")
    client = build_dacon_openai_client(settings) if args.live else None
    report["configured_model"] = settings.openai_model
    runs: list[dict[str, object]] = []
    try:
        for case in (1, 2):
            source, _ = case_fixture(case)
            prompt = decision_payload_json(source)
            metadata = calculate_metadata(source["evidence"])
            bound = len((INSTRUCTIONS + prompt).encode()) + 512
            if bound > 32000:
                raise ValueError("decision_input_budget_exceeded")
            for attempt in range(1, 4 if client else 2):
                row: dict[str, object] = {
                    "case": "Palbociclib" if case == 1 else "Abemaciclib",
                    "attempt": attempt,
                    "candidate_count": len(metadata.candidate_gates),
                    "started_at_utc": datetime.now(UTC).isoformat(),
                    "input_bytes": len(prompt.encode()),
                    "input_budget_bound": bound,
                    "input_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "lead_candidate": metadata.lead_candidate.model_dump(mode="json")
                    if metadata.lead_candidate
                    else None,
                }
                if client:
                    try:
                        generated = await client.generate_text(
                            prompt,
                            instructions=INSTRUCTIONS,
                            max_output_tokens=DECISION_MAX_OUTPUT_TOKENS,
                            output_schema=DecisionAssessmentGeneration,
                        )
                    except Exception as error:
                        row.update(status="model_error", error=model_call_diagnostic(error))
                    else:
                        row.update(
                            model=generated.model,
                            response_status=generated.status,
                            incomplete_reason=generated.incomplete_reason,
                            refused=generated.refused,
                            usage=asdict(generated.usage) if generated.usage else None,
                            raw_output=generated.text,
                        )
                        try:
                            assessment = validate_assessment(
                                generated.text,
                                generated.status,
                                source["evidence"],
                                bool(metadata.go_restrictions),
                                allow_recall=source.get("allow_recall", False),
                                metadata=metadata,
                                boundary=source["dta_reference_boundary"],
                            )
                            warnings = validate_policy(
                                assessment.model_dump(mode="json"),
                                source["evidence"],
                                metadata,
                                source["dta_reference_boundary"],
                            )
                            assessment, markers = remove_inline_evidence_markers(assessment)
                            assessment, numbers = normalize_reader_numbers(assessment)
                            row.update(
                                status="accepted",
                                verdict=assessment.verdict,
                                warning_codes=warnings,
                                removed_inline_markers=markers,
                                normalized_numbers=numbers,
                                output=assessment.model_dump(mode="json"),
                            )
                        except InvalidDecisionOutput as error:
                            row.update(status="rejected", rejection_code=str(error))
                else:
                    row["status"] = "dry_run_not_model_execution"
                row["finished_at_utc"] = datetime.now(UTC).isoformat()
                runs.append(row)
                # 중단·응답 유실 시에도 완료한 호출 기록을 남기고 재시작하지 않는다.
                report["runs"] = runs
                with args.report.open("w", encoding="utf-8") as checkpoint:
                    json.dump(report, checkpoint, ensure_ascii=False, indent=2)
                print(
                    json.dumps(
                        {k: v for k, v in row.items() if k not in {"raw_output", "output"}},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    finally:
        if client:
            await client.close()
    report["runs"] = runs
    report["finished_at_utc"] = datetime.now(UTC).isoformat()
    with args.report.open("w", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print(f"report: {args.report.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
