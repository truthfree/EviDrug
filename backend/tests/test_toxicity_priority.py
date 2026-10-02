"""원값 경계·결측·세 축 및 구형/신규 공개 저장 계약을 검증한다."""

from uuid import uuid4

import pytest

from evidrug_api.admet.agent import AdmetAgentResult
from evidrug_api.admet.context import AdmetContext, AdmetContextRow
from evidrug_api.admet.contracts import AdmetTaskType
from evidrug_api.admet.toxicity import calculate_toxicity_axes
from evidrug_api.analysis_jobs.results import project_admet
from evidrug_api.orchestration.reasoning import ADME_INSTRUCTIONS, TOXICITY_INSTRUCTIONS


def context(value: float = 0.5, percentile: float = 90.0) -> AdmetContext:
    return AdmetContext(
        source_tool_call_id=uuid4(),
        source_run_id=uuid4(),
        manifest_sha256="a" * 64,
        tool_version="test",
        endpoint_metadata_sha256="b" * 64,
        reference_population="DrugBank approved",
        reference_sha256="c" * 64,
        catalog_endpoint_count=4,
        limitations=(),
        is_partial=False,
        rows=tuple(
            AdmetContextRow(
                endpoint,
                endpoint,
                "toxicity",
                AdmetTaskType.CLASSIFICATION,
                value,
                None,
                percentile,
                None,
                None,
            )
            for endpoint in ("DILI", "hERG", "AMES", "LD50_Zhu")
        ),
    )


@pytest.mark.parametrize("endpoint", ["DILI", "hERG", "AMES"])
@pytest.mark.parametrize(
    "value,percentile,expected",
    [
        (0.5, 90.0, "priority_check"),
        (0.4999, 90.0, "not_priority"),
        (0.5, 89.99, "not_priority"),
        (0.98, 99.0, "priority_check"),
    ],
)
def test_axes_use_unrounded_values_and_both_thresholds(
    endpoint: str,
    value: float,
    percentile: float,
    expected: str,
) -> None:
    source = context(value, percentile)
    axes = calculate_toxicity_axes(source)
    axis = next(item for item in axes.axes if item.endpoint_id == endpoint)
    assert axis.priority_status == expected
    assert (axis.value, axis.drugbank_approved_percentile) == (value, percentile)
    assert axis.source_tool_call_id == source.source_tool_call_id
    assert len(axes.axes) == 3 and all(a.endpoint_id != "LD50_Zhu" for a in axes.axes)


def test_missing_axes_do_not_get_safe_defaults() -> None:
    source = context()
    axes = calculate_toxicity_axes(source, ("hERG",))
    assert axes.axes[1].priority_status == "missing"
    assert axes.axes[1].value is None and axes.axes[1].source_tool_call_id is None
    reduced = source.model_copy(update={"rows": (source.rows[0],), "is_partial": True})
    assert calculate_toxicity_axes(reduced).axes[2].priority_status == "missing"
    assert all(a.priority_status == "missing" for a in calculate_toxicity_axes(None).axes)


def test_new_and_legacy_results_keep_public_round_trip_contract() -> None:
    source = context()
    legacy = AdmetAgentResult(context=source, interpretation=None)
    assert project_admet(legacy).toxicity_axes is None
    result = legacy.model_copy(update={"toxicity_axes": calculate_toxicity_axes(source)})
    stored = AdmetAgentResult.model_validate_json(result.model_dump_json())
    public = project_admet(stored)
    assert public.toxicity_axes == result.toxicity_axes


def test_prompt_uses_supplied_priority_without_adme_benchmark_requirement() -> None:
    assert "do not recompute it" in TOXICITY_INSTRUCTIONS
    assert "Never write that a molecule is safe or has no toxicity" in TOXICITY_INSTRUCTIONS
    assert "benchmark" not in ADME_INSTRUCTIONS.casefold()
