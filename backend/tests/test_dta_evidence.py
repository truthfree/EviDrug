from uuid import uuid4

import pytest

from evidrug_api.analysis_input.models import PotencyCriterion, PotencyEndpoint
from evidrug_api.dta.agent import DtaModelRun
from evidrug_api.dta.contracts import SCORE_UNITS, DtaModel, DtaObservation, DtaScoreType
from evidrug_api.dta.evidence import (
    AssayEvidence,
    AssayEvidenceKind,
    AssaySource,
    EvidenceRelationshipStatus,
    PubchemRecallTrigger,
    assess_dta_evidence,
)


def criterion() -> PotencyCriterion:
    return PotencyCriterion(endpoint=PotencyEndpoint.KD, maximum_value=100, unit="nM")


def run(tool_id: str, pkd: float) -> DtaModelRun:
    return DtaModelRun(
        tool_id=tool_id,
        tool_call_id=uuid4(),
        status="succeeded",
        model=DtaModel(provider="test", model_id=tool_id, version="1"),
        observations=(
            DtaObservation(
                score_type=DtaScoreType.PREDICTED_PKD,
                value=pkd,
                unit=SCORE_UNITS[DtaScoreType.PREDICTED_PKD],
            ),
        ),
    )


def assay(source: AssaySource, record_id: str, value_nm: float) -> AssayEvidence:
    return AssayEvidence(
        source=source,
        source_record_id=record_id,
        kind=AssayEvidenceKind.QUANTITATIVE,
        endpoint=PotencyEndpoint.KD,
        value=value_nm,
        unit="nM",
    )


def test_model_difference_without_criterion_is_not_decision_relevant() -> None:
    result = assess_dta_evidence((run("cnn", 6.5), run("mpnn", 7.5)), (), None)

    assert result.status is EvidenceRelationshipStatus.INSUFFICIENT_EXPERIMENTAL_EVIDENCE
    assert result.recall_trigger is None
    assert result.pubchem_requested is False


def test_models_on_different_criterion_sides_request_pubchem() -> None:
    result = assess_dta_evidence(
        (run("cnn", 6.5), run("mpnn", 7.5)),
        (assay(AssaySource.BINDINGDB, "b1", 80),),
        criterion(),
    )

    assert result.status is EvidenceRelationshipStatus.DECISION_RELEVANT_DISAGREEMENT
    assert result.recall_trigger is PubchemRecallTrigger.MODEL_DECISION_REGION_DISAGREEMENT
    assert result.pubchem_requested is True


def test_database_conflict_does_not_request_pubchem_without_model_split() -> None:
    result = assess_dta_evidence(
        (run("cnn", 7.5), run("mpnn", 7.2)),
        (
            assay(AssaySource.BINDINGDB, "b1", 50),
            assay(AssaySource.CHEMBL, "c1", 500),
        ),
        criterion(),
    )

    assert result.status is EvidenceRelationshipStatus.EXPERIMENT_SOURCE_CONFLICT
    assert result.recall_trigger is None
    assert result.pubchem_requested is False


@pytest.mark.parametrize(
    "values,expected",
    [
        ((6.5, 7.4), "split_across_reference"),
        ((4.8, 6.6), "same_region_below_reference"),
        ((7.2, 7.8), "same_region_meets_reference"),
        ((7.0, 6.9), "split_across_reference"),
        ((7.4,), "insufficient_model_results"),
    ],
)
def test_boundary_controls_request(values: tuple[float, ...], expected: str) -> None:
    result = assess_dta_evidence(
        tuple(run(str(i), v) for i, v in enumerate(values)), (), criterion()
    )
    assert result.region_status == expected
    assert result.pubchem_requested == (expected == "split_across_reference")


@pytest.mark.parametrize(
    "values,support", [((20, 100), True), ((101, 500), False), ((20, 500), False), ((), False)]
)
def test_quantitative_kd_support_is_independent(values: tuple[int, ...], support: bool) -> None:
    records = tuple(assay(AssaySource.PUBCHEM, str(i), v) for i, v in enumerate(values))
    result = assess_dta_evidence(
        (run("cnn", 6.5), run("mpnn", 7.4)), records, criterion(), pubchem_executed=True
    )
    assert result.experimental_binding_support is support
    assert result.region_status == "split_across_reference"
    assert result.pubchem_requested


def test_below_reference_concordance_is_not_binding_support() -> None:
    result = assess_dta_evidence(
        (run("cnn", 6.5), run("mpnn", 6.6)), (assay(AssaySource.PUBCHEM, "a", 500),), criterion()
    )
    assert result.status is EvidenceRelationshipStatus.CONCORDANT
    assert not result.experimental_binding_support


def test_non_kd_criterion_and_ki_records_do_not_support_binding() -> None:
    item = assay(AssaySource.PUBCHEM, "a", 20).model_copy(update={"endpoint": PotencyEndpoint.KI})
    result = assess_dta_evidence((run("cnn", 6.5), run("mpnn", 7.4)), (item,), criterion())
    assert not result.experimental_binding_support
    result = assess_dta_evidence(
        (run("cnn", 6.5), run("mpnn", 7.4)),
        (),
        criterion().model_copy(update={"endpoint": PotencyEndpoint.IC50}),
    )
    assert result.region_status == "insufficient_model_results"
    assert not result.pubchem_requested


def test_qualitative_pubchem_result_does_not_resolve_quantitative_gap() -> None:
    qualitative = AssayEvidence(
        source=AssaySource.PUBCHEM,
        source_record_id="aid:1",
        kind=AssayEvidenceKind.QUALITATIVE,
        qualitative_outcome="Active",
    )

    result = assess_dta_evidence(
        (run("cnn", 7.5), run("mpnn", 7.2)),
        (qualitative,),
        criterion(),
        pubchem_executed=True,
    )

    assert result.status is EvidenceRelationshipStatus.INSUFFICIENT_EXPERIMENTAL_EVIDENCE
    assert result.pubchem_executed is True
    assert result.pubchem_resolved is False


def test_unit_conversion_compares_equivalent_values() -> None:
    evidence = AssayEvidence(
        source=AssaySource.CHEMBL,
        source_record_id="c1",
        kind=AssayEvidenceKind.QUANTITATIVE,
        endpoint=PotencyEndpoint.KD,
        value=0.05,
        unit="uM",
    )

    result = assess_dta_evidence((run("cnn", 7.5), run("mpnn", 7.2)), (evidence,), criterion())

    assert result.status is EvidenceRelationshipStatus.CONCORDANT
    assert result.comparable_record_ids == ("c1",)
