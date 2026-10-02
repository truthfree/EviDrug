"""첨부 재구성본을 실제 projection 경로로 바꾸는 테스트 전용 fixture."""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from evidrug_api.admet.context import AdmetContext, AdmetContextRow
from evidrug_api.admet.toxicity import calculate_toxicity_axes
from evidrug_api.decision.policy import calculate_metadata
from evidrug_api.dta.contracts import SCORE_UNITS, DtaObservation, DtaScoreType

FIXTURES = Path(__file__).parent / "fixtures" / "decision_v5_7"


def case_fixture(case: int = 1) -> tuple[dict[str, Any], dict[str, Any]]:
    name = "palbociclib" if case == 1 else "abemaciclib"
    source = json.loads(next(FIXTURES.glob(f"시험입력_{case}_{name}_*.json")).read_text())
    output = json.loads(next(FIXTURES.glob(f"결과_{case}_{name}_*.json")).read_text())
    evidence = source["evidence"]
    admet = evidence["admet:context"]
    rows = admet.pop("rows")
    for row in rows:
        row["drugbank_approved_percentile"] = row.pop("approved_drug_percentile")
        row["species"] = None
        row["source_url"] = None
    context = AdmetContext(
        source_tool_call_id=uuid4(),
        source_run_id=uuid4(),
        manifest_sha256="0" * 64,
        tool_version="fixture-v1",
        endpoint_metadata_sha256="0" * 64,
        reference_population=admet["reference_population"],
        reference_sha256="0" * 64,
        catalog_endpoint_count=len(rows),
        limitations=(),
        rows=tuple(AdmetContextRow(**r) for r in rows),
        is_partial=False,
    )
    admet["context"] = context.model_dump(mode="json")
    admet["toxicity_axes"] = calculate_toxicity_axes(context).model_dump(mode="json")
    for key, dta in evidence.items():
        if not key.startswith("dta:") or key == "dta:interpretation":
            continue
        for run in dta["model_runs"]:
            run["tool_id"] = run["model_id"]
            # 임의 문자열 fixture 대신 실제 provider 출력 계약으로 직렬화한다.
            run["observations"] = [
                DtaObservation(
                    score_type=DtaScoreType.PREDICTED_PKD,
                    value=float(run.pop("predicted_pkd")),
                    unit=SCORE_UNITS[DtaScoreType.PREDICTED_PKD],
                ).model_dump(mode="json")
            ]
    lead = calculate_metadata(evidence).lead_candidate
    assert lead is not None
    source["lead_candidate"] = lead.model_dump(mode="json")
    return source, output


def add_new_fields(payload: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """테스트용 생성 모델이 v5.7 필드를 명시적으로 출력하도록 한다."""
    payload.update(
        headline="합성 근거를 검토했습니다.",
        key_strengths=[],
        key_concerns=[],
        assessment={
            area: {"status": status, "summary": "합성 근거를 검토했습니다.", "key_values": []}
            for area, status in calculate_metadata(evidence).expected_area_statuses.items()
        },
    )
    return payload
