from copy import deepcopy

import pytest
from test_ctoxpred2_adapter import provider_report

from evidrug_api.admet.contracts import (
    AdmetEndpointDefinition,
    AdmetEndpointPrediction,
    AdmetModelArtifact,
    AdmetModelManifest,
    AdmetNormalizedOutput,
    AdmetTaskType,
    AdmetToolResult,
)
from evidrug_api.ctoxpred2.adapter import normalize_ctox_report
from evidrug_api.ctoxpred2.contracts import CtoxChannel
from evidrug_api.ctoxpred2.relationship import relate_admet_and_ctox


def admet_output(*, include_herg: bool = True) -> AdmetNormalizedOutput:
    endpoints = (
        AdmetEndpointDefinition(
            endpoint_id="hERG" if include_herg else "AMES",
            category="Toxicity",
            name="hERG" if include_herg else "Ames",
            task_type=AdmetTaskType.CLASSIFICATION,
        ),
    )
    predictions = (
        AdmetEndpointPrediction(
            endpoint_id=endpoints[0].endpoint_id,
            value=0.8,
            drugbank_approved_percentile=95,
        ),
    )
    manifest = AdmetModelManifest(
        manifest_sha256="a" * 64,
        tool_version="1.4.0",
        endpoint_metadata_sha256="b" * 64,
        reference_population="DrugBank approved",
        reference_sha256="c" * 64,
        model_artifacts=(AdmetModelArtifact(relative_path="model.pkl", sha256="d" * 64),),
        endpoints=endpoints,
    )
    return AdmetNormalizedOutput(
        manifest=manifest,
        result=AdmetToolResult(
            manifest_sha256=manifest.manifest_sha256,
            canonical_smiles="CCO",
            predictions=predictions,
        ),
    )


def test_herg_never_becomes_concordant_or_discordant() -> None:
    ctox = normalize_ctox_report(provider_report(), expected_smiles="CCO")
    report = relate_admet_and_ctox(admet_output(), ctox)
    rows = {item.ctox_channel: item for item in report.relationships}

    assert rows[CtoxChannel.HERG].status == "unresolved"
    assert rows[CtoxChannel.HERG].reason_code == "decision_criteria_unverified"
    assert rows[CtoxChannel.HERG].baseline_value == 0.8
    assert rows[CtoxChannel.NAV1_5].status == "related_signal"
    assert rows[CtoxChannel.CAV1_2].baseline_endpoint_id is None
    assert "averaging" in report.limitations[2]


def test_missing_baseline_herg_remains_unresolved() -> None:
    ctox = normalize_ctox_report(provider_report(), expected_smiles="CCO")
    report = relate_admet_and_ctox(admet_output(include_herg=False), ctox)
    herg = next(item for item in report.relationships if item.ctox_channel is CtoxChannel.HERG)
    assert herg.status == "unresolved"
    assert herg.reason_code == "baseline_endpoint_unavailable"


def test_relationship_rejects_different_molecule() -> None:
    raw = deepcopy(provider_report())
    raw["canonical_smiles"] = "CCC"
    ctox = normalize_ctox_report(raw)
    with pytest.raises(ValueError, match="same canonical SMILES"):
        relate_admet_and_ctox(admet_output(), ctox)
