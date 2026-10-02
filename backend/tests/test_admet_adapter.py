"""ADMET-AI report의 manifest/prediction 정규화와 실패 정책 검증."""

from copy import deepcopy
from typing import Any

import pytest

from evidrug_api.admet import (
    AdmetReportError,
    AdmetTaskType,
    AdmetToolAdapter,
    AdmetToolArguments,
    normalize_admet_report,
)

SMILES = "CC(=O)Oc1ccccc1C(=O)O"


def endpoint_metadata(
    endpoint_id: str,
    *,
    task_type: str,
    minimum: str,
    maximum: str,
) -> dict[str, str]:
    """실제 ADMET-AI CSV와 같은 문자열 기반 metadata 행을 만든다."""

    return {
        "category": "Toxicity" if task_type == "classification" else "Absorption",
        "id": endpoint_id,
        "name": "Mutagenicity" if endpoint_id == "AMES" else "Caco-2 Permeability",
        "size": "7255" if endpoint_id == "AMES" else "906",
        "task_type": task_type,
        "units": "-" if endpoint_id == "AMES" else "log cm/s",
        "minimum": minimum,
        "maximum": maximum,
        "species": "salmonella typhimurium" if endpoint_id == "AMES" else "human",
        "tdc_rank": "2" if endpoint_id == "AMES" else "",
        "AUPRC": "0.912265988" if endpoint_id == "AMES" else "",
        "AUROC": "0.892359338" if endpoint_id == "AMES" else "",
        "R^2": "" if endpoint_id == "AMES" else "0.58",
        "MAE": "" if endpoint_id == "AMES" else "0.41",
        "url": "https://tdcommons.ai/single_pred_tasks/tox/#ames-mutagenicity",
    }


def provider_report() -> dict[str, Any]:
    """불필요한 smoke metadata도 포함한 최소 정상 provider report를 만든다."""

    return {
        "status": "passed",
        "scope": "installation_load_single_inference_only",
        "smiles": SMILES,
        "python": "3.12.13",
        "packages": {"admet-ai": "1.4.0", "torch": "2.5.0+cpu"},
        "model_sha256": {
            "admet_classification/model_0.pt": "a" * 64,
            "admet_regression/model_0.pt": "b" * 64,
        },
        "reference_population": "bundled DrugBank approved, all ATC groups",
        "reference_sha256": "c" * 64,
        "endpoint_metadata_sha256": "d" * 64,
        "endpoint_metadata": [
            endpoint_metadata(
                "AMES",
                task_type="classification",
                minimum="0",
                maximum="1",
            ),
            endpoint_metadata(
                "Caco2_Wang",
                task_type="regression",
                minimum="-10",
                maximum="inf",
            ),
        ],
        "predictions": {
            "AMES": 0.05,
            "AMES_drugbank_approved_percentile": 19.85,
            "Caco2_Wang": -4.4,
            "Caco2_Wang_drugbank_approved_percentile": 86.35,
        },
        "limitations": [
            "Smoke test only; not predictive accuracy or clinical validation.",
            "DrugBank percentiles are not prediction confidence.",
        ],
    }


def test_normalizes_stable_manifest_and_compact_prediction_rows() -> None:
    """반복 metadata와 실행별 값이 서로 다른 계약으로 분리된다."""

    normalized = normalize_admet_report(provider_report(), expected_smiles=SMILES)

    manifest = normalized.manifest
    result = normalized.result
    assert manifest.tool_version == "1.4.0"
    assert result.manifest_sha256 == manifest.manifest_sha256
    assert [endpoint.endpoint_id for endpoint in manifest.endpoints] == [
        "AMES",
        "Caco2_Wang",
    ]
    assert manifest.endpoints[0].task_type is AdmetTaskType.CLASSIFICATION
    assert manifest.endpoints[0].units is None
    assert manifest.endpoints[1].maximum is None
    assert manifest.endpoints[1].maximum_unbounded is True
    assert result.predictions[0].drugbank_approved_percentile == 19.85

    result_payload = result.model_dump(mode="json")
    assert set(result_payload["predictions"][0]) == {
        "endpoint_id",
        "value",
        "drugbank_approved_percentile",
    }
    assert "category" not in result.model_dump_json()
    assert "DrugBank percentiles are not prediction confidence." in manifest.limitations


def test_manifest_identifier_is_deterministic_across_model_hash_order() -> None:
    """동일한 산출물은 provider dictionary 순서와 무관하게 같은 manifest를 참조한다."""

    first = provider_report()
    second = deepcopy(first)
    second["model_sha256"] = dict(reversed(list(first["model_sha256"].items())))

    first_output = normalize_admet_report(first)
    second_output = normalize_admet_report(second)

    assert first_output.manifest.manifest_sha256 == second_output.manifest.manifest_sha256


def test_manifest_identifier_covers_normalized_endpoint_metadata() -> None:
    """provider가 같은 file hash를 주장해도 실제 metadata 변경은 manifest에 반영된다."""

    first = provider_report()
    second = deepcopy(first)
    second["endpoint_metadata"][0]["name"] = "Changed endpoint name"

    first_output = normalize_admet_report(first)
    second_output = normalize_admet_report(second)

    assert first_output.manifest.manifest_sha256 != second_output.manifest.manifest_sha256


@pytest.mark.parametrize(
    ("missing_key", "unexpected_key"),
    [
        ("AMES", None),
        ("AMES_drugbank_approved_percentile", None),
        (None, "unknown_endpoint"),
    ],
)
def test_rejects_prediction_keys_that_do_not_match_catalog(
    missing_key: str | None,
    unexpected_key: str | None,
) -> None:
    """endpoint 또는 percentile 누락과 catalog 밖의 값을 조용히 저장하지 않는다."""

    report = provider_report()
    if missing_key is not None:
        del report["predictions"][missing_key]
    if unexpected_key is not None:
        report["predictions"][unexpected_key] = 0.5

    with pytest.raises(AdmetReportError, match="prediction keys"):
        normalize_admet_report(report)


@pytest.mark.parametrize("percentile", [-0.1, 100.1, float("nan"), True])
def test_rejects_invalid_drugbank_percentile(percentile: object) -> None:
    """percentile 범위와 수치 타입을 provider 경계에서 강제한다."""

    report = provider_report()
    report["predictions"]["AMES_drugbank_approved_percentile"] = percentile

    with pytest.raises(AdmetReportError):
        normalize_admet_report(report)


def test_rejects_out_of_range_classification_value() -> None:
    """classification probability가 0~1 밖이면 성공 결과로 만들지 않는다."""

    report = provider_report()
    report["predictions"]["AMES"] = 1.1

    with pytest.raises(AdmetReportError, match="classification prediction"):
        normalize_admet_report(report)


def test_rejects_duplicate_endpoint_metadata() -> None:
    """동일 endpoint metadata 두 행이 prediction 하나를 공유하지 못하게 한다."""

    report = provider_report()
    report["endpoint_metadata"].append(deepcopy(report["endpoint_metadata"][0]))

    with pytest.raises(AdmetReportError, match="duplicate ids"):
        normalize_admet_report(report)


def test_wraps_invalid_endpoint_metadata_as_provider_error() -> None:
    """공개 adapter 경계 밖으로 내부 Pydantic 오류를 노출하지 않는다."""

    report = provider_report()
    report["endpoint_metadata"][0]["AUPRC"] = "1.1"

    with pytest.raises(AdmetReportError, match="normalized contract"):
        normalize_admet_report(report)


def test_rejects_report_for_different_smiles() -> None:
    """provider가 다른 분자의 cached 결과를 반환하면 연결을 중단한다."""

    with pytest.raises(AdmetReportError, match="different SMILES"):
        normalize_admet_report(provider_report(), expected_smiles="CCO")


class FakeProvider:
    """실제 모델 없이 공통 비동기 tool surface를 검증한다."""

    def __init__(self) -> None:
        self.received_smiles: str | None = None

    async def predict(self, canonical_smiles: str) -> dict[str, Any]:
        self.received_smiles = canonical_smiles
        return provider_report()


@pytest.mark.asyncio
async def test_tool_adapter_uses_same_contract_for_fixed_and_agent_modes() -> None:
    """실행 방식과 무관한 arguments/result 경계를 제공한다."""

    provider = FakeProvider()
    adapter = AdmetToolAdapter(provider=provider)

    output = await adapter.execute(AdmetToolArguments(canonical_smiles=SMILES))

    assert provider.received_smiles == SMILES
    assert output.result.canonical_smiles == SMILES
