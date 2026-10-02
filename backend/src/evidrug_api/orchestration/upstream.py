"""참조된 저장 Agent 출력만 hash·소유권·입력·typed schema를 검증해 읽는다."""

import hashlib

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evidrug_api.execution_contracts.agent import AgentInput, AgentOutput, UpstreamOutputReference
from evidrug_api.orchestration.replay_contracts import ReplayError
from evidrug_api.orchestration.replay_store import ReplayStore
from evidrug_api.orchestration.tables import AgentRunRecord


class InvalidUpstream(ValueError):
    """저장 결과의 참조 또는 입력 무결성이 맞지 않는다."""


async def load_upstream[T: BaseModel](
    factory: async_sessionmaker[AsyncSession],
    agent_input: AgentInput,
    reference: UpstreamOutputReference,
    output_type: type[AgentOutput[T]],
) -> AgentOutput[T]:
    """현재 분석의 성공/부분 성공 출력만 읽으며 외부 호출과 fallback은 하지 않는다."""
    artifact = reference.output
    allowed_analysis_id = agent_input.analysis_id
    if (
        artifact.artifact_id != reference.run_id
        or artifact.schema_name != "agent_output"
        or artifact.schema_version != "1"
        or artifact.media_type != "application/json"
    ):
        raise InvalidUpstream("upstream_reference_mismatch")
    async with factory() as session:
        run = await session.get(AgentRunRecord, reference.run_id)
        if run is not None and run.analysis_id != agent_input.analysis_id:
            # 개발 continuation의 명시적 원본 두 개에만 교차 분석 읽기를 허용한다.
            # 일반 분석, 단독 replay, 임의로 추가한 참조에는 이 권한이 없다.
            try:
                plan = await ReplayStore(factory, {}).load(agent_input.analysis_id)
            except ReplayError:
                raise InvalidUpstream("upstream_unavailable") from None
            if (
                plan.request.mode != "continue_dta_decision"
                or agent_input.agent_name not in {name for name in ReplayStore.stage_names(plan)}
                or plan.case_input != agent_input.case_input
                or not any(
                    item.run_id == reference.run_id
                    and item.agent_name == reference.agent_name
                    and item.output_sha256 == artifact.sha256
                    for item in plan.upstream
                )
            ):
                raise InvalidUpstream("upstream_continuation_not_authorized")
            allowed_analysis_id = plan.request.base_analysis_id
        if (
            run is None
            or run.analysis_id != allowed_analysis_id
            or run.agent_name != reference.agent_name
            or run.status not in {"completed", "partial_failure"}
            or run.finished_at is None
            or run.output_json is None
            or run.input_json is None
        ):
            raise InvalidUpstream("upstream_unavailable")
        if (
            hashlib.sha256(run.output_json.encode()).hexdigest() != run.output_sha256
            or run.output_sha256 != artifact.sha256
            or hashlib.sha256(run.input_json.encode()).hexdigest() != run.input_sha256
        ):
            raise InvalidUpstream("upstream_hash_mismatch")
        saved_input = AgentInput.model_validate_json(run.input_json)
        output = output_type.model_validate_json(run.output_json)
        if (
            saved_input.case_input != agent_input.case_input
            or saved_input.analysis_id != run.analysis_id
            or saved_input.run_id != run.run_id
            or saved_input.agent_name != run.agent_name
            or output.analysis_id != run.analysis_id
            or output.run_id != run.run_id
            or output.agent_name != run.agent_name
            or output.status != run.status
            or output.result is None
        ):
            raise InvalidUpstream("upstream_identity_mismatch")
        return output
