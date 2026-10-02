"""고정 실행과 Agent tool에서 재사용하는 비동기 DTA provider 경계."""

import asyncio
import math
from time import perf_counter
from typing import Protocol

from evidrug_api.dta.contracts import DtaArguments, DtaModel, DtaObservation, DtaResult


class DtaProviderUnavailable(RuntimeError):
    """provider 구현이 연결 실패·rate limit 등 예상 가능한 장애를 정규화한다."""


class DtaProvider(Protocol):
    """모델 실행은 별도 런타임에서 수행하고 취소 가능한 I/O만 이 경계에 둔다."""

    @property
    def model(self) -> DtaModel: ...

    async def predict(self, arguments: DtaArguments) -> tuple[DtaObservation, ...]:
        """요청한 입력의 관측값을 반환하거나 DtaProviderUnavailable을 발생시킨다."""
        ...


class DtaToolAdapter:
    """예상 가능한 장애만 unavailable로 변환하며 취소·구현 오류는 전파한다."""

    def __init__(self, provider: DtaProvider, *, timeout_seconds: float = 120) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout must be positive and finite")
        self.provider = provider
        self.timeout_seconds = timeout_seconds

    async def execute(self, arguments: DtaArguments) -> DtaResult:
        """시간 제한 안에 관측값을 얻으며 실패 원문을 결과에 노출하지 않는다."""

        started = perf_counter()
        model = self.provider.model
        try:
            async with asyncio.timeout(self.timeout_seconds):
                observations = await self.provider.predict(arguments)
        except (TimeoutError, DtaProviderUnavailable) as error:
            return DtaResult(
                model=model,
                arguments=arguments,
                status="unavailable",
                error_code=(
                    "provider_timeout"
                    if isinstance(error, TimeoutError)
                    else "provider_unavailable"
                ),
                duration_seconds=perf_counter() - started,
            )
        return DtaResult(
            model=model,
            arguments=arguments,
            status="succeeded",
            observations=observations,
            duration_seconds=perf_counter() - started,
        )
