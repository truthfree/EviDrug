"""Decision replay가 참조하는 실제 전문 Agent run의 불변 출처 검증."""

import hashlib
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.admet.agent import AdmetAgentResult
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.decision.dta_lineage import (
    DtaAssayEvidenceReference,
    verify_dta_assay_lineage,
)
from evidrug_api.decision.projection import admet_evidence, dta_evidence, target_evidence
from evidrug_api.dta.agent import DtaAgentResult
from evidrug_api.execution_contracts.agent import AgentInput, AgentOutput
from evidrug_api.execution_contracts.common import ContractModel
from evidrug_api.orchestration.tables import AgentRunRecord
from evidrug_api.orchestration.upstream import InvalidUpstream
from evidrug_api.target_hypothesis.contracts import TargetHypothesisResult
from evidrug_api.trajectory.admet_snapshot import AdmetObservationSnapshot


class DecisionSourceError(RuntimeError):
    """원본 run의 누락·변조·계약 불일치를 외부 호출 없이 거부한다."""


class DecisionSourceReference(ContractModel):
    run_id: UUID
    agent_name: AnalysisStageName
    attempt: Literal[1] = 1
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_version: Literal["1"] = "1"
    implementation_version: str = Field(min_length=1, max_length=200)


class DecisionSourceManifest(ContractModel):
    schema_version: Literal["1"] = "1"
    analysis_id: UUID
    target: DecisionSourceReference
    admet: DecisionSourceReference
    dta: DecisionSourceReference

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        if (
            self.target.agent_name is not AnalysisStageName.TARGET_HYPOTHESIS
            or self.admet.agent_name is not AnalysisStageName.ADMET
            or self.dta.agent_name is not AnalysisStageName.DTA
            or len({self.target.run_id, self.admet.run_id, self.dta.run_id}) != 3
        ):
            raise ValueError("decision source roles must be distinct and ordered")
        return self


class VerifiedDecisionSources(ContractModel):
    target: AgentOutput[TargetHypothesisResult]
    admet: AgentOutput[AdmetAgentResult]
    dta: AgentOutput[DtaAgentResult]
    target_input: AgentInput
    assay_lineage: tuple[DtaAssayEvidenceReference, ...] = ()


async def verify_decision_sources(
    session: AsyncSession,
    manifest: DecisionSourceManifest,
    admet_snapshot: AdmetObservationSnapshot,
) -> VerifiedDecisionSources:
    """DB의 정확한 run과 snapshot 연결을 확인하며 live fallback은 하지 않는다."""
    analysis = await session.get(AnalysisRecord, manifest.analysis_id)
    if analysis is None:
        raise DecisionSourceError("decision_source_analysis_missing")
    if admet_snapshot.analysis_id != manifest.analysis_id or len(admet_snapshot.entries) != 1:
        raise DecisionSourceError("decision_source_snapshot_mismatch")

    async def load[T: BaseModel](
        reference: DecisionSourceReference, output_type: type[AgentOutput[T]]
    ) -> tuple[AgentInput, AgentOutput[T]]:
        row = await session.get(AgentRunRecord, reference.run_id)
        if row is None:
            raise DecisionSourceError(f"decision_source_missing:{reference.agent_name.value}")
        if (
            row.analysis_id != manifest.analysis_id
            or row.agent_name != reference.agent_name
            or row.attempt != reference.attempt
            or row.status not in {"completed", "partial_failure"}
            or row.finished_at is None
            or row.input_json is None
            or row.output_json is None
        ):
            raise DecisionSourceError(
                f"decision_source_state_mismatch:{reference.agent_name.value}"
            )
        if (
            row.input_sha256 != reference.input_sha256
            or row.output_sha256 != reference.output_sha256
            or hashlib.sha256(row.input_json.encode()).hexdigest() != reference.input_sha256
            or hashlib.sha256(row.output_json.encode()).hexdigest() != reference.output_sha256
        ):
            raise DecisionSourceError(f"decision_source_hash_mismatch:{reference.agent_name.value}")
        try:
            saved_input = AgentInput.model_validate_json(row.input_json)
            output = output_type.model_validate_json(row.output_json)
        except ValidationError as error:
            raise DecisionSourceError(
                f"decision_source_schema_mismatch:{reference.agent_name.value}"
            ) from error
        if (
            output.schema_version != reference.output_schema_version
            or output.execution_metadata.implementation_version != reference.implementation_version
        ):
            raise DecisionSourceError(
                f"decision_source_version_mismatch:{reference.agent_name.value}"
            )
        if (
            saved_input.analysis_id != manifest.analysis_id
            or saved_input.run_id != row.run_id
            or saved_input.agent_name != row.agent_name
            or saved_input.attempt != row.attempt
            or saved_input.case_input.disease_id != analysis.disease_id
            or saved_input.case_input.disease_name != analysis.disease_name
            or saved_input.case_input.original_smiles != analysis.original_smiles
            or saved_input.case_input.canonical_smiles != analysis.canonical_smiles
            or output.analysis_id != manifest.analysis_id
            or output.run_id != row.run_id
            or output.agent_name != row.agent_name
            or output.status != row.status
            or output.result is None
        ):
            raise DecisionSourceError(
                f"decision_source_identity_mismatch:{reference.agent_name.value}"
            )
        return saved_input, output

    target_input, target = await load(manifest.target, AgentOutput[TargetHypothesisResult])
    admet_input, admet = await load(manifest.admet, AgentOutput[AdmetAgentResult])
    dta_input, dta = await load(manifest.dta, AgentOutput[DtaAgentResult])
    assert target.result is not None and admet.result is not None and dta.result is not None
    if (
        target_input.case_input != admet_input.case_input
        or target_input.case_input != dta_input.case_input
        or target_input.case_input.target_mode != analysis.target_mode
        or target_input.case_input.target_name != analysis.target_name
    ):
        raise DecisionSourceError("decision_source_case_mismatch")
    if (
        len(dta_input.upstream_outputs) != 1
        or dta_input.upstream_outputs[0].run_id != target.run_id
        or dta_input.upstream_outputs[0].output.sha256 != manifest.target.output_sha256
    ):
        raise DecisionSourceError("decision_source_dta_input_mismatch")
    if dta.result.source_target_run_id != target.run_id:
        raise DecisionSourceError("decision_source_dta_lineage_mismatch")
    if (
        admet.result.context.source_run_id != admet.run_id
        or admet.result.context.source_tool_call_id != admet_snapshot.entries[0].source_tool_call_id
        or admet_snapshot.entries[0].source_run_id != admet.run_id
    ):
        raise DecisionSourceError("decision_source_admet_snapshot_mismatch")
    try:
        assay_lineage = await verify_dta_assay_lineage(
            session,
            analysis_id=manifest.analysis_id,
            run_id=dta.run_id,
            canonical_smiles=target_input.case_input.canonical_smiles,
            result=dta.result,
        )
        target_evidence(target)
        admet_evidence(admet)
        dta_evidence(dta, target, assay_lineage)
    except InvalidUpstream as error:
        raise DecisionSourceError(f"decision_source_projection_mismatch:{error}") from error
    return VerifiedDecisionSources(
        target=target,
        admet=admet,
        dta=dta,
        target_input=target_input,
        assay_lineage=assay_lineage,
    )
