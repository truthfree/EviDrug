"""저장 결과의 출처·입력·스키마를 검증하고 단독 실행 계획을 저장한다."""

import hashlib
from collections.abc import Callable, Mapping
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evidrug_api.analysis_input.models import AnalysisInputResponse
from evidrug_api.analysis_jobs.models import AnalysisEventType, AnalysisStageName, AnalysisStatus
from evidrug_api.analysis_jobs.tables import (
    AnalysisEventRecord,
    AnalysisRecord,
    AnalysisStageRecord,
)
from evidrug_api.execution_contracts.agent import AgentInput, AgentOutput, AgentOutputStatus
from evidrug_api.execution_contracts.common import ComponentVersion, ExecutionMetadata
from evidrug_api.orchestration.contracts import ExecutionProfile
from evidrug_api.orchestration.replay_contracts import (
    ReplayError,
    ReplayManifest,
    ReplayRequest,
    StoredRunIdentity,
)
from evidrug_api.orchestration.repository import OrchestrationRepository
from evidrug_api.orchestration.tables import (
    AgentReplayRecord,
    AgentRunRecord,
    AnalysisExecutionRecord,
)

OutputParser = Callable[[str], AgentOutput[BaseModel]]
OutputParsers = Mapping[AnalysisStageName, OutputParser]

REQUIRED_UPSTREAM: dict[AnalysisStageName, tuple[AnalysisStageName, ...]] = {
    AnalysisStageName.TARGET_HYPOTHESIS: (),
    AnalysisStageName.ADMET: (),
    AnalysisStageName.DTA: (AnalysisStageName.TARGET_HYPOTHESIS,),
    AnalysisStageName.DECISION: (
        AnalysisStageName.TARGET_HYPOTHESIS,
        AnalysisStageName.ADMET,
        AnalysisStageName.DTA,
    ),
}


def digest(serialized: str) -> str:
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ReplayStore:
    """외부 요청을 하지 않는 fail-closed replay 준비·조회 경계."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], parsers: OutputParsers) -> None:
        self.factory = factory
        self.parsers = parsers

    async def manifest(
        self,
        request: ReplayRequest,
        runtime_versions: tuple[ComponentVersion, ...] = (),
    ) -> ReplayManifest:
        """성공 upstream만 허용하며 과거 부족한 근거를 live 조회로 보충하지 않는다."""
        if request.mode == "target_from_snapshot":
            raise ReplayError("replay_evidence_snapshot_missing")
        async with self.factory() as session:
            analysis = await session.get(AnalysisRecord, request.base_analysis_id)
            if analysis is None:
                raise ReplayError("replay_analysis_missing")
            if analysis.status in {AnalysisStatus.QUEUED, AnalysisStatus.RUNNING}:
                raise ReplayError("replay_analysis_not_terminal")
            case = OrchestrationRepository._case_input(analysis)
            source = await self._run(session, request.source_run_id, request.base_analysis_id)
            if source.agent_name != request.agent_name:
                raise ReplayError("replay_agent_mismatch")
            source_input = await self._input(session, source, case)
            if source.status in {"running"}:
                raise ReplayError("replay_source_not_terminal")
            if request.mode == "reuse_output" or source.status == "completed":
                self.parse_output(source)

            upstream = [
                await self._run(session, run_id, request.base_analysis_id)
                for run_id in request.upstream_run_ids
            ]
            required = REQUIRED_UPSTREAM[request.agent_name]
            if request.mode == "continue_dta_decision":
                required = (AnalysisStageName.TARGET_HYPOTHESIS, AnalysisStageName.ADMET)
            if {run.agent_name for run in upstream} != set(required) or len(upstream) != len(
                required
            ):
                raise ReplayError("replay_upstream_set_mismatch")
            upstream.sort(key=lambda run: required.index(AnalysisStageName(run.agent_name)))
            for run in upstream:
                self.parse_output(run)
                saved_input = await self._input(session, run, case)
                self._validate_lineage(saved_input, upstream)
            if request.mode == "reuse_output":
                source_input = await self._input(session, source, case)
                self._validate_lineage(source_input, upstream)
            return ReplayManifest(
                request=request,
                case_input=case,
                case_sha256=digest(case.model_dump_json()),
                source=self._identity(source),
                upstream=tuple(self._identity(run) for run in upstream),
                runtime_versions=runtime_versions,
                profile=ExecutionProfile(
                    profile_id="poc-continuation",
                    policy_version="dta-decision-continuation-v1",
                    stage_limits=source_input.execution_limits,
                )
                if request.mode == "continue_dta_decision"
                else ExecutionProfile(
                    profile_id="agent-replay",
                    policy_version="single-agent-v1",
                ),
            )

    async def prepare(self, analysis_id: UUID, manifest: ReplayManifest) -> UUID:
        """새 단독 분석과 계획을 원자 저장한다. 같은 ID·계획은 재사용한다."""
        # 호출자가 조립한 manifest도 검증한 저장 결과와 일치해야 한다.
        if await self.manifest(manifest.request, manifest.runtime_versions) != manifest:
            raise ReplayError("replay_manifest_mismatch")
        serialized = manifest.model_dump_json()
        async with self.factory() as session:
            existing = await session.get(AgentReplayRecord, analysis_id)
            if existing is not None:
                self._match_manifest(existing, serialized)
                return analysis_id
            if await session.get(AnalysisRecord, analysis_id) is not None:
                raise ReplayError("replay_analysis_id_conflict")
            session.add(
                AnalysisRecord(
                    id=analysis_id,
                    session_fingerprint=digest("agent-replay-cli-v1"),
                    idempotency_key=str(analysis_id),
                    **manifest.case_input.model_dump(exclude={"potency_criterion"}),
                    potency_endpoint=(
                        manifest.case_input.potency_criterion.endpoint.value
                        if manifest.case_input.potency_criterion is not None
                        else None
                    ),
                    potency_maximum_value=(
                        manifest.case_input.potency_criterion.maximum_value
                        if manifest.case_input.potency_criterion is not None
                        else None
                    ),
                    potency_unit=(
                        manifest.case_input.potency_criterion.unit
                        if manifest.case_input.potency_criterion is not None
                        else None
                    ),
                    stages=[
                        AnalysisStageRecord(name=name, position=index)
                        for index, name in enumerate(self.stage_names(manifest), start=1)
                    ],
                    events=[
                        AnalysisEventRecord(
                            event_type=AnalysisEventType.CREATED,
                            status=AnalysisStatus.QUEUED,
                        )
                    ],
                )
            )
            try:
                await session.flush()
                session.add(
                    AgentReplayRecord(
                        analysis_id=analysis_id,
                        base_analysis_id=manifest.request.base_analysis_id,
                        source_run_id=manifest.request.source_run_id,
                        manifest_json=serialized,
                        manifest_sha256=digest(serialized),
                    )
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.get(AgentReplayRecord, analysis_id)
                if existing is None:
                    raise ReplayError("replay_analysis_id_conflict") from None
                self._match_manifest(existing, serialized)
        return analysis_id

    async def load(self, analysis_id: UUID) -> ReplayManifest:
        """저장 계획과 단독 분석 입력의 일치 여부를 확인한다."""
        async with self.factory() as session:
            record = await session.get(AgentReplayRecord, analysis_id)
            analysis = await session.get(AnalysisRecord, analysis_id)
            if record is None or analysis is None:
                raise ReplayError("replay_plan_missing")
            if digest(record.manifest_json) != record.manifest_sha256:
                raise ReplayError("replay_manifest_hash_mismatch")
            try:
                manifest = ReplayManifest.model_validate_json(record.manifest_json)
            except ValidationError:
                raise ReplayError("replay_schema_mismatch") from None
            if (
                manifest.request.base_analysis_id != record.base_analysis_id
                or manifest.request.source_run_id != record.source_run_id
                or OrchestrationRepository._case_input(analysis) != manifest.case_input
                or digest(manifest.case_input.model_dump_json()) != manifest.case_sha256
            ):
                raise ReplayError("replay_input_mismatch")
            stages = list(
                await session.scalars(
                    select(AnalysisStageRecord.name).where(
                        AnalysisStageRecord.analysis_id == analysis_id
                    )
                )
            )
            if set(stages) != set(self.stage_names(manifest)):
                raise ReplayError("replay_stage_mismatch")
            return manifest

    @staticmethod
    def stage_names(manifest: ReplayManifest) -> tuple[AnalysisStageName, ...]:
        if manifest.request.mode == "continue_dta_decision":
            return (AnalysisStageName.DTA, AnalysisStageName.DECISION)
        return (manifest.request.agent_name,)

    async def validate(self, analysis_id: UUID) -> ReplayManifest:
        """실행 직전 원본을 다시 검증해 준비 이후 변조도 차단한다."""
        saved = await self.load(analysis_id)
        if saved != await self.manifest(saved.request, saved.runtime_versions):
            raise ReplayError("replay_source_changed")
        return saved

    def parse_output(self, run: AgentRunRecord) -> AgentOutput[BaseModel]:
        """schema별 parser가 없는 결과는 범용 dict로 승인하지 않는다."""
        if run.status != "completed" or run.output_json is None or run.finished_at is None:
            raise ReplayError("replay_output_not_completed")
        if digest(run.output_json) != run.output_sha256:
            raise ReplayError("replay_output_hash_mismatch")
        parser = self.parsers.get(AnalysisStageName(run.agent_name))
        if parser is None:
            raise ReplayError("replay_schema_not_registered")
        try:
            output = parser(run.output_json)
        except ValidationError:
            raise ReplayError("replay_schema_mismatch") from None
        if (
            output.analysis_id != run.analysis_id
            or output.run_id != run.run_id
            or output.agent_name != run.agent_name
            or output.status is not AgentOutputStatus.COMPLETED
        ):
            raise ReplayError("replay_output_identity_mismatch")
        if self._identity(run).metadata != output.execution_metadata:
            raise ReplayError("replay_metadata_mismatch")
        return output

    async def _input(
        self, session: AsyncSession, run: AgentRunRecord, case: AnalysisInputResponse
    ) -> AgentInput:
        serialized = run.input_json
        if serialized is None:
            # 이전 버전의 독립 단계만 완전히 복원 가능하다. 추정 upstream은 허용하지 않는다.
            if run.agent_name not in {AnalysisStageName.TARGET_HYPOTHESIS, AnalysisStageName.ADMET}:
                raise ReplayError("replay_input_snapshot_missing")
            execution = await session.get(AnalysisExecutionRecord, run.analysis_id)
            if execution is None or digest(execution.profile_json) != execution.profile_sha256:
                raise ReplayError("replay_profile_missing")
            try:
                profile = ExecutionProfile.model_validate_json(execution.profile_json)
            except ValidationError:
                raise ReplayError("replay_schema_mismatch") from None
            serialized = AgentInput(
                analysis_id=run.analysis_id,
                run_id=run.run_id,
                agent_name=AnalysisStageName(run.agent_name),
                attempt=run.attempt,
                case_input=case,
                execution_limits=profile.stage_limits,
            ).model_dump_json()
        if digest(serialized) != run.input_sha256:
            raise ReplayError("replay_input_hash_mismatch")
        try:
            saved = AgentInput.model_validate_json(serialized)
        except ValidationError:
            raise ReplayError("replay_schema_mismatch") from None
        if (
            saved.case_input != case
            or saved.analysis_id != run.analysis_id
            or saved.run_id != run.run_id
            or saved.agent_name != run.agent_name
            or saved.attempt != run.attempt
        ):
            raise ReplayError("replay_input_mismatch")
        return saved

    @staticmethod
    def _validate_lineage(saved: AgentInput, upstream: list[AgentRunRecord]) -> None:
        # Decision이 서로 다른 Target에서 파생된 DTA 결과를 섞지 못한다.
        required = REQUIRED_UPSTREAM[saved.agent_name]
        if {ref.agent_name for ref in saved.upstream_outputs} != set(required) or len(
            saved.upstream_outputs
        ) != len(required):
            raise ReplayError("replay_upstream_lineage_mismatch")
        by_run = {run.run_id: run for run in upstream}
        for reference in saved.upstream_outputs:
            matching = by_run.get(reference.run_id)
            if (
                matching is None
                or reference.output.sha256 != matching.output_sha256
                or reference.agent_name != matching.agent_name
                or reference.output.artifact_id != matching.run_id
                or reference.output.schema_name != "agent_output"
                or reference.output.schema_version != "1"
                or reference.output.media_type != "application/json"
            ):
                raise ReplayError("replay_upstream_lineage_mismatch")

    @staticmethod
    async def _run(session: AsyncSession, run_id: UUID, base_id: UUID) -> AgentRunRecord:
        run = await session.get(AgentRunRecord, run_id)
        if run is None:
            raise ReplayError("replay_run_missing")
        if run.analysis_id != base_id:
            raise ReplayError("replay_origin_mismatch")
        return run

    @staticmethod
    def _identity(run: AgentRunRecord) -> StoredRunIdentity:
        try:
            metadata = (
                ExecutionMetadata.model_validate_json(run.execution_metadata_json)
                if run.execution_metadata_json is not None
                else None
            )
            return StoredRunIdentity(
                run_id=run.run_id,
                agent_name=AnalysisStageName(run.agent_name),
                input_sha256=run.input_sha256,
                output_sha256=run.output_sha256,
                status=AgentOutputStatus(run.status),
                error_code=run.error_code,
                metadata=metadata,
            )
        except ValueError:
            raise ReplayError("replay_schema_mismatch") from None

    @staticmethod
    def _match_manifest(existing: AgentReplayRecord, serialized: str) -> None:
        if existing.manifest_json != serialized or existing.manifest_sha256 != digest(serialized):
            raise ReplayError("replay_analysis_id_conflict")
