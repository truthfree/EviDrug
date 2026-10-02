"""영역 상태·수치·라벨·문체를 원 근거와 대조하는 v5.7 검증."""

import math
import re
from dataclasses import dataclass

from evidrug_api.admet.agent import ADME_ENDPOINTS, TOXICITY_ENDPOINTS
from evidrug_api.decision.policy import DecisionMetadata, admet_rows, items, mapping
from evidrug_api.dta.contracts import DtaScoreType


class PolicyViolation(ValueError):
    """응답 내용이 없는 고정 검증 코드."""


@dataclass(frozen=True)
class AllowedValue:
    value: float
    unit: str | None = None
    metric: str = ""
    symbol: str = ""


MOLAR_FACTORS = {"pM": 1e-12, "nM": 1e-9, "uM": 1e-6, "µM": 1e-6, "μM": 1e-6, "mM": 1e-3, "M": 1.0}
LABEL_RULES = (
    (("mpnn-cnn", "mpnn_cnn"), "mpnn"),
    (("cnn-cnn", "cnn_cnn"), "cnn"),
    (("herg",), "hERG"),
    (("dili", "간손상", "간 손상"), "DILI"),
    (("ames", "변이원성", "돌연변이원성"), "AMES"),
    (("nav1.5",), "nav1_5"),
    (("cav1.2",), "cav1_2"),
    (("기준선", "비교 기준"), "boundary"),
)
INTERNAL_TERMS = re.compile(
    r"(?<![A-Za-z0-9_])(?:gate|flagged|region_status|same_region_meets_reference|same_region_below_reference|"
    r"split_across_reference|insufficient_model_results|comparison_boundary|priority_status|"
    r"priority_check|not_priority|lead_candidate|experimental_binding_support|go|conditional_go|no_go)(?![A-Za-z0-9_])|관문",
    re.IGNORECASE,
)


def matches(raw: float, displayed: float) -> bool:
    """정수·소수 1/2자리 또는 유효숫자 두 자리 표시를 허용한다."""
    candidates = [round(raw, digits) for digits in (0, 1, 2)] + [float(format(raw, ".2g"))]
    return any(math.isclose(v, displayed, rel_tol=1e-12, abs_tol=0.0) for v in candidates)


def allowed_values(
    evidence: dict[str, object],
    boundary: dict[str, object] | None,
) -> dict[str, list[AllowedValue]]:
    """값마다 단위·지표·후보 출처를 보존해 라벨 교차 대입을 막는다."""
    result: dict[str, list[AllowedValue]] = {
        area: [] for area in ("target", "dta", "adme", "safety")
    }
    for key, raw in evidence.items():
        row = mapping(raw)
        if key.startswith("target:"):
            value = row.get("association")
            if isinstance(value, (float, int)):
                result["target"].append(
                    AllowedValue(float(value), symbol=str(row.get("symbol", "")))
                )
        if not key.startswith("dta:") or key == "dta:interpretation":
            continue
        symbol = str(row.get("approved_symbol", ""))
        for run in items(row.get("model_runs")):
            model = mapping(run)
            identifier = str(model.get("tool_id", "")) + str(
                mapping(model.get("model")).get("model_id", "")
            )
            metric = "mpnn" if "mpnn" in identifier.lower() else "cnn"
            for observation in items(model.get("observations")):
                obs = mapping(observation)
                pkd = obs.get("value")
                if obs.get("score_type") != DtaScoreType.PREDICTED_PKD or not isinstance(
                    pkd, (float, int)
                ):
                    continue
                result["dta"].append(AllowedValue(float(pkd), metric=metric, symbol=symbol))
                for unit, factor in MOLAR_FACTORS.items():
                    result["dta"].append(
                        AllowedValue(10 ** (-float(pkd)) / factor, unit, metric, symbol)
                    )
        records = items(mapping(row.get("experimental_evidence_summary")).get("key_records"))
        for raw_record in records:
            record = mapping(raw_record)
            value, record_unit = record.get("value"), record.get("unit")
            if (
                record.get("kind") == "quantitative"
                and record.get("endpoint") == "Kd"
                and isinstance(value, (float, int))
                and record_unit in MOLAR_FACTORS
            ):
                for display_unit, factor in MOLAR_FACTORS.items():
                    result["dta"].append(
                        AllowedValue(
                            float(value) * MOLAR_FACTORS[str(record_unit)] / factor,
                            display_unit,
                            "assay",
                            symbol,
                        )
                    )
    if boundary and boundary.get("endpoint") == "Kd":
        for field, boundary_unit in (("pkd", None), ("value", str(boundary["unit"]))):
            value = boundary.get(field)
            if isinstance(value, (float, int)):
                result["dta"].append(AllowedValue(float(value), boundary_unit, "boundary"))
    for row in admet_rows(evidence):
        endpoint = str(row.get("endpoint_id"))
        area = (
            "adme"
            if endpoint in ADME_ENDPOINTS
            else "safety"
            if endpoint in TOXICITY_ENDPOINTS
            else None
        )
        if area is None:
            continue
        for field in ("value", "drugbank_approved_percentile"):
            value = row.get(field)
            if isinstance(value, (float, int)):
                row_unit = str(row["units"]) if field == "value" and row.get("units") else None
                result[area].append(AllowedValue(float(value), row_unit, endpoint))
    for raw in items(mapping(evidence.get("admet:cardiac_ion_channels")).get("predictions")):
        pred = mapping(raw)
        value = pred.get("class_probability")
        if isinstance(value, (float, int)):
            metric = "hERG" if pred.get("channel") == "herg" else str(pred.get("channel"))
            result["safety"].append(AllowedValue(float(value), metric=metric + ":ctox"))
    return result


def validate_policy(
    output: dict[str, object],
    evidence: dict[str, object],
    metadata: DecisionMetadata,
    boundary: dict[str, object] | None = None,
) -> tuple[str, ...]:
    """값/판정 오류는 거부하고 문체/출처 표시 문제는 경고로 반환한다."""
    verdict = output["verdict"]
    expected = metadata.expected_area_statuses
    if verdict == "go" and (
        metadata.go_restrictions
        or any(expected[a] != "supported" for a in ("target", "dta", "safety"))
    ):
        raise PolicyViolation("decision_go_gate_unresolved")
    if verdict == "no_go" and (
        not boundary
        or boundary.get("endpoint") != "Kd"
        or not any(
            mapping(record).get("endpoint") == "Kd"
            and mapping(record).get("kind") == "quantitative"
            for key, raw in evidence.items()
            if key.startswith("dta:")
            for record in items(
                mapping(mapping(raw).get("experimental_evidence_summary")).get("key_records")
            )
        )
    ):
        raise PolicyViolation("decision_no_go_experimental_evidence_missing")
    warnings: list[str] = []
    values = allowed_values(evidence, boundary)
    lead = metadata.lead_candidate
    prose = [str(output.get("headline", "")), str(output["rationale"])]
    for field in ("key_strengths", "key_concerns", "conflicts", "gaps"):
        prose.extend(str(v) for v in items(output.get(field)))
    for action in items(output["next_actions"]):
        prose.extend(
            str(mapping(action).get(k, "")) for k in ("action", "rationale", "decision_impact")
        )
    for area, raw in mapping(output["assessment"]).items():
        section = mapping(raw)
        if section["status"] != expected[area]:
            raise PolicyViolation("decision_area_status_mismatch")
        summary = str(section["summary"])
        prose.append(summary)
        if (
            lead
            and len(metadata.candidate_gates) > 1
            and area in ("target", "dta")
            and lead.symbol.casefold() not in summary.casefold()
        ):
            warnings.append("decision_lead_candidate_summary_missing")
        for raw_value in items(section["key_values"]):
            entry = mapping(raw_value)
            displayed = float(str(entry["value"]))
            label = str(entry["label"]).casefold()
            unit = entry.get("unit")
            matching = [v for v in values[area] if v.unit == unit and matches(v.value, displayed)]
            if not matching:
                raise PolicyViolation("decision_key_value_out_of_range")
            metric = next(
                (
                    m
                    for names, m in LABEL_RULES
                    if any(n in label for n in names) and (m != "boundary" or area == "dta")
                ),
                None,
            )
            filtered = values[area]
            if metric:
                filtered = [v for v in filtered if v.metric in (metric, metric + ":ctox")]
                if metric in ("cnn", "mpnn"):
                    named = next(
                        (
                            g.symbol
                            for g in metadata.candidate_gates
                            if g.symbol.casefold() in label
                        ),
                        None,
                    )
                    symbol = named or (lead.symbol if lead else "")
                    filtered = [v for v in filtered if v.symbol == symbol]
                if not any(v.unit == unit and matches(v.value, displayed) for v in filtered):
                    raise PolicyViolation("decision_key_value_label_mismatch")
            if metric == "hERG" and all(v.metric == "hERG:ctox" for v in matching):
                warnings.append("decision_ctox_herg_value_ambiguous")
            if (
                lead
                and len(metadata.candidate_gates) > 1
                and area in ("target", "dta")
                and all(v.symbol and v.symbol != lead.symbol for v in matching)
                and not any(g.symbol.casefold() in label for g in metadata.candidate_gates)
            ):
                warnings.append("decision_non_lead_value_unlabelled")
    if any(INTERNAL_TERMS.search(sentence) for sentence in prose):
        warnings.append("decision_internal_terms_in_prose")
    return tuple(dict.fromkeys(warnings))
