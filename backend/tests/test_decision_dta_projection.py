import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from evidrug_api.analysis_input.models import PotencyCriterion, PotencyEndpoint
from evidrug_api.decision.dta_lineage import DtaAssayEvidenceReference
from evidrug_api.decision.projection import dta_evidence
from evidrug_api.dta.agent import DtaAgentResult, DtaCandidateResult, DtaModelRun
from evidrug_api.dta.contracts import DtaModel, DtaObservation, DtaScoreType
from evidrug_api.dta.evidence import (
    AssayEvidence,
    AssayEvidenceKind,
    AssaySource,
    DtaEvidenceAssessment,
    EvidenceRelationshipStatus,
)
from evidrug_api.execution_contracts.agent import AgentOutput, AgentOutputStatus
from evidrug_api.orchestration.upstream import InvalidUpstream
from evidrug_api.target_hypothesis.contracts import TargetHypothesisResult


def test_dta_decision_projection_is_bounded_and_keeps_sql_lineage() -> None:
    target_run_id = uuid4()
    dta_run_id = uuid4()
    sequence_hash = "a" * 64
    chembl_records = tuple(
        AssayEvidence(
            source=AssaySource.CHEMBL,
            source_record_id=f"CHEMBL_ACTIVITY_{position:03d}_" + "x" * 120,
            kind=AssayEvidenceKind.QUANTITATIVE,
            endpoint=PotencyEndpoint.KD,
            value=float(position + 1),
            unit="nM",
            assay_description="large provider description " + "z" * 900,
            doi="10.1000/test",
        )
        for position in range(50)
    )
    records = (
        *chembl_records,
        AssayEvidence(
            source=AssaySource.BINDINGDB,
            source_record_id="binding-opposes",
            kind=AssayEvidenceKind.QUANTITATIVE,
            endpoint=PotencyEndpoint.KD,
            value=120,
            unit="nM",
            doi="10.1000/binding",
        ),
        AssayEvidence(
            source=AssaySource.PUBCHEM,
            source_record_id="pubchem-supports",
            kind=AssayEvidenceKind.QUANTITATIVE,
            endpoint=PotencyEndpoint.KD,
            value=80,
            unit="nM",
            doi="10.1000/pubchem",
        ),
        AssayEvidence(
            source=AssaySource.BINDINGDB,
            source_record_id="cross-source-duplicate",
            kind=AssayEvidenceKind.QUANTITATIVE,
            endpoint=PotencyEndpoint.KD,
            value=1,
            unit="nM",
            doi="10.1000/test",
        ),
    )
    model = DtaModel(provider="test", model_id="cnn", version="1")
    observation = DtaObservation(
        score_type=DtaScoreType.PREDICTED_PKD,
        value=7.0,
        unit="-log10(Kd [M])",
    )
    model_tool_call_id = uuid4()
    candidate = DtaCandidateResult(
        ensembl_id="ENSG00000135446",
        approved_symbol="CDK4",
        uniprot_accession="P11802",
        target_sequence_sha256=sequence_hash,
        tool_call_id=model_tool_call_id,
        status="succeeded",
        model=model,
        observations=(observation,),
        model_runs=(
            DtaModelRun(
                tool_id="dta",
                tool_call_id=model_tool_call_id,
                status="succeeded",
                model=model,
                observations=(observation,),
            ),
        ),
        experimental_evidence=records,
        evidence_assessment=DtaEvidenceAssessment(
            status=EvidenceRelationshipStatus.CONCORDANT,
            criterion=PotencyCriterion(endpoint=PotencyEndpoint.KD, maximum_value=100, unit="nM"),
            comparable_record_ids=tuple(item.source_record_id for item in records),
        ),
    )
    dta_result = DtaAgentResult(
        source_target_run_id=target_run_id,
        candidates=(candidate,),
    )
    dta_output = cast(
        AgentOutput[DtaAgentResult],
        SimpleNamespace(
            result=dta_result,
            run_id=dta_run_id,
            status=AgentOutputStatus.COMPLETED,
        ),
    )
    target_output = cast(
        AgentOutput[TargetHypothesisResult],
        SimpleNamespace(
            run_id=target_run_id,
            result=SimpleNamespace(
                primary=SimpleNamespace(
                    ensembl_id=candidate.ensembl_id,
                    target_sequence_sha256=sequence_hash,
                ),
                alternatives=(),
            ),
        ),
    )
    tool_call_id = uuid4()
    lineage = tuple(
        DtaAssayEvidenceReference(
            ensembl_id=candidate.ensembl_id,
            source=item.source,
            source_record_id=item.source_record_id,
            source_tool_call_id=tool_call_id,
        )
        for item in records
    )

    with pytest.raises(InvalidUpstream, match="decision_dta_assay_policy_mismatch"):
        dta_evidence(dta_output, target_output, lineage)
    # 신규 정책 결과에 과거 출처가 섞여도 거부한다.
    dta_result = dta_result.model_copy(update={"assay_policy_version": "pubchem-recall-v1"})
    dta_output.result = dta_result
    with pytest.raises(InvalidUpstream, match="decision_dta_assay_policy_mismatch"):
        dta_evidence(dta_output, target_output, lineage)
    candidate = candidate.model_copy(
        update={
            "experimental_evidence": tuple(
                item.model_copy(update={"source": AssaySource.PUBCHEM}) for item in records
            )
        }
    )
    dta_output.result = dta_result.model_copy(update={"candidates": (candidate,)})
    dta_output.error = None
    lineage = tuple(item.model_copy(update={"source": AssaySource.PUBCHEM}) for item in lineage)
    projected, restricted = dta_evidence(dta_output, target_output, lineage)

    item = cast(dict[str, Any], projected["dta:" + candidate.ensembl_id])
    summary = cast(dict[str, Any], item["experimental_evidence_summary"])
    assert "evidence_assessment" not in item
    assert restricted is False
    assert summary["raw_count"] == 53
    assert summary["deduplicated_count"] == 52
    assert summary["duplicate_count"] == 1
    assert summary["comparable_count"] == 52
    assert summary["supports_criterion_count"] == 51
    assert summary["opposes_criterion_count"] == 1
    assert summary["criterion_value_distribution"] == {
        "unit": "nM",
        "minimum": 1.0,
        "median": 26.5,
        "maximum": 120.0,
    }
    assert summary["included_count"] == 15
    assert summary["omitted_deduplicated_count"] == 37
    assert len(summary["key_records"]) == 15
    assert all(record["selection_reasons"] for record in summary["key_records"])
    assert {record["source"] for record in summary["key_records"]} == {
        "pubchem",
    }
    assert any(
        "prediction_experiment_conflict" in record["selection_reasons"]
        for record in summary["key_records"]
    )
    assert all(
        record["source_tool_call_id"] == str(tool_call_id) for record in summary["key_records"]
    )
    assert "experimental_evidence" not in item
    assert "target_sequence_sha256" not in item
    serialized = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    assert "large provider description" not in serialized
    assert len(serialized.encode()) < 12_000
