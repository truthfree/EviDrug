"""명시적 등록 목록에서 schema 공개와 서버 실행 검증을 함께 파생한다."""

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.tool_admission.contracts import RunContext, ToolRunPolicy


@dataclass(frozen=True)
class Invocation:
    context: RunContext
    request_id: UUID
    tool_call_id: UUID
    timeout_seconds: float
    session: AsyncSession


@dataclass(frozen=True)
class ToolBinding:
    """서버 코드에 등록한 schema·실행 함수. 임의 import나 URL을 허용하지 않는다."""

    tool_id: str
    version: str
    description: str
    agents: tuple[AnalysisStageName, ...]
    arguments_type: type[BaseModel]
    result_type: type[BaseModel]
    invoke: Callable[[Invocation, BaseModel], Awaitable[BaseModel]]
    max_timeout_seconds: float


class ToolRegistry:
    """생성 후 등록 목록을 고정한다. 새 provider는 binding으로 연결한다."""

    def __init__(self, bindings: tuple[ToolBinding, ...]) -> None:
        entries = {(item.tool_id, item.version): item for item in bindings}
        if len(entries) != len(bindings):
            raise ValueError("duplicate tool registration")
        if any(
            not item.tool_id
            or not item.version
            or not item.agents
            or not math.isfinite(item.max_timeout_seconds)
            or item.max_timeout_seconds <= 0
            for item in bindings
        ):
            raise ValueError("invalid tool registration")
        self._entries = MappingProxyType(entries)

    def validate_policy(self, policy: ToolRunPolicy) -> None:
        """설정 오류는 run 생성 시 fail closed한다."""
        for pin in policy.allowed_tools:
            binding = self.get(pin.tool_id, pin.version)
            if binding is None or policy.context.agent not in binding.agents:
                raise ValueError("policy references unavailable or forbidden tool")

    def get(self, tool_id: str, version: str) -> ToolBinding | None:
        return self._entries.get((tool_id, version))

    def schemas(self, policy: ToolRunPolicy) -> tuple[dict[str, object], ...]:
        """SDK 중립 schema다. 서버 admission과 동일한 allowlist를 사용한다."""
        self.validate_policy(policy)
        return tuple(
            {
                "tool_id": binding.tool_id,
                "version": binding.version,
                "description": binding.description,
                "parameters": binding.arguments_type.model_json_schema(),
            }
            for pin in policy.allowed_tools
            if (binding := self.get(pin.tool_id, pin.version)) is not None
        )

    def check(
        self, policy: ToolRunPolicy, tool_id: str, version: str, arguments: BaseModel
    ) -> tuple[ToolBinding | None, BaseModel | None, str | None]:
        """권한·고정 버전·입력 검증 실패에는 공개 가능한 코드만 반환한다."""
        pin = next((pin for pin in policy.allowed_tools if pin.tool_id == tool_id), None)
        if pin is None:
            return None, None, "tool_not_allowed"
        if pin.version != version:
            return None, None, "tool_version_mismatch"
        binding = self.get(tool_id, version)
        if binding is None:
            return None, None, "tool_unavailable"
        if policy.context.agent not in binding.agents:
            return None, None, "tool_not_allowed"
        try:
            parsed = binding.arguments_type.model_validate(arguments.model_dump(mode="json"))
        except ValidationError:
            return None, None, "invalid_arguments"
        return binding, parsed, None
