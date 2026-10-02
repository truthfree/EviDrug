"""adaptive 선택은 capability와 서버가 고정한 권한·예산만 사용한다."""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_tool_capabilities import binding

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.ctoxpred2.contracts import CTOX_TOOL_VERSION
from evidrug_api.tool_admission.capabilities import (
    EvidenceGap,
    EvidenceGapKind,
    EvidenceKind,
    ResourceClass,
    ToolCapability,
    capabilities_for_bindings,
)
from evidrug_api.tool_admission.contracts import ToolPin
from evidrug_api.trajectory.adaptive_selector import (
    AdaptiveSelectorInput,
    SelectionReason,
    select_next_tool,
)


def selector_input(
    *,
    kind: EvidenceGapKind = EvidenceGapKind.CARDIAC_ION_CHANNEL,
    allowed: bool = True,
    calls: int = 1,
    inputs: frozenset[str] = frozenset({"canonical_smiles"}),
    max_cost: ResourceClass = ResourceClass.HIGH,
    max_latency: ResourceClass = ResourceClass.HIGH,
) -> AdaptiveSelectorInput:
    bindings = (
        binding("admet_ai", "1.4.0", AnalysisStageName.ADMET),
        binding("ctoxpred2", CTOX_TOOL_VERSION, AnalysisStageName.ADMET),
    )
    return AdaptiveSelectorInput(
        policy_version="adaptive-cardiac-v1",
        gap=EvidenceGap(
            gap_id=uuid4(),
            kind=kind,
            objective="심장 이온통로 예측 근거를 확보한다.",
            target_context="candidate molecule",
            required_evidence_kind=EvidenceKind.MODEL_PREDICTION,
            endpoint_id="herg",
            expected_field="class_probability",
            priority=2,
        ),
        agent=AnalysisStageName.ADMET,
        capabilities=capabilities_for_bindings(bindings),
        allowed_tools=(ToolPin(tool_id="ctoxpred2", version=CTOX_TOOL_VERSION),) if allowed else (),
        available_inputs=tuple(sorted(inputs)),
        remaining_tool_calls=calls,
        max_cost_class=max_cost,
        max_latency_class=max_latency,
    )


def test_selector_is_order_independent_and_preserves_match_reasons() -> None:
    config = selector_input()
    first = select_next_tool(config)
    reversed_catalog = select_next_tool(
        config.model_copy(update={"capabilities": tuple(reversed(config.capabilities))})
    )
    assert first == reversed_catalog
    assert first.selected_action is not None
    assert first.selected_action.tool_id == "ctoxpred2"
    assert [(item.tool_id, item.reason_code) for item in first.candidates] == [
        ("admet_ai", "unsupported_gap_kind"),
        ("ctoxpred2", "selected"),
    ]


def test_selector_input_serialization_is_canonical_across_input_order() -> None:
    base = selector_input()
    payload = base.model_dump()
    first = AdaptiveSelectorInput.model_validate(
        payload | {"available_inputs": ["canonical_smiles", "target_sequence"]}
    )
    reversed_input = AdaptiveSelectorInput.model_validate(
        payload
        | {
            "capabilities": tuple(reversed(base.capabilities)),
            "allowed_tools": tuple(reversed(base.allowed_tools)),
            "available_inputs": ["target_sequence", "canonical_smiles"],
        }
    )
    assert first.model_dump_json() == reversed_input.model_dump_json()
    assert select_next_tool(first) == select_next_tool(reversed_input)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"allowed": False}, SelectionReason.NOT_ALLOWED),
        ({"calls": 0}, SelectionReason.TOOL_BUDGET_EXHAUSTED),
        ({"inputs": frozenset()}, "missing_required_input"),
        ({"max_cost": ResourceClass.LOW}, SelectionReason.COST_LIMIT_EXCEEDED),
        ({"max_latency": ResourceClass.MEDIUM}, SelectionReason.LATENCY_LIMIT_EXCEEDED),
        ({"kind": EvidenceGapKind.CLINICAL_CARDIAC_SAFETY}, "explicitly_out_of_scope"),
    ],
)
def test_selector_fails_closed_for_scope_permissions_and_resources(
    change: dict[str, object], reason: str
) -> None:
    result = select_next_tool(selector_input(**change))  # type: ignore[arg-type]
    assert result.selected_action is None
    assert result.reason_code is SelectionReason.NO_MATCH
    ctox = next(item for item in result.candidates if item.tool_id == "ctoxpred2")
    assert ctox.reason_code == reason


def test_version_pin_blocks_other_version_and_tie_break_is_stable() -> None:
    config = selector_input()
    ctox = next(item for item in config.capabilities if item.tool_id == "ctoxpred2")
    alternate = ToolCapability.model_validate(
        ctox.model_dump()
        | {
            "capability_id": "cardiac_ion_channels:alternate",
            "tool_id": "cardiac_alternate",
        }
    )
    with_alternate = config.model_copy(
        update={
            "capabilities": (*config.capabilities, alternate),
            "allowed_tools": (
                *config.allowed_tools,
                ToolPin(tool_id="cardiac_alternate", version=ctox.tool_version),
            ),
        }
    )
    selected = select_next_tool(with_alternate)
    assert selected.selected_action is not None
    assert selected.selected_action.tool_id == "cardiac_alternate"
    assert [item.reason_code for item in selected.candidates if item.selected] == ["selected"]
    assert [item.reason_code for item in selected.candidates if item.tool_id == "ctoxpred2"] == [
        "lower_ranked"
    ]
    wrong_pin = config.model_copy(
        update={"allowed_tools": (ToolPin(tool_id="ctoxpred2", version="other"),)}
    )
    denied = select_next_tool(wrong_pin)
    assert denied.selected_action is None
    assert denied.candidates[-1].reason_code == SelectionReason.VERSION_NOT_PINNED


def test_selector_rejects_duplicate_capability_or_tool_pin() -> None:
    config = selector_input()
    with pytest.raises(ValidationError, match="capability IDs"):
        AdaptiveSelectorInput.model_validate(
            config.model_dump() | {"capabilities": (*config.capabilities, config.capabilities[0])}
        )
    with pytest.raises(ValidationError, match="one version per tool"):
        AdaptiveSelectorInput.model_validate(
            config.model_dump()
            | {"allowed_tools": (*config.allowed_tools, config.allowed_tools[0])}
        )
