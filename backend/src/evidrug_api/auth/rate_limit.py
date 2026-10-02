from collections.abc import Awaitable
from dataclasses import dataclass
from hashlib import sha256
from typing import Final, Protocol, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

RATE_LIMIT_KEY_PREFIX: Final = "evidrug:auth-attempts:"


class RateLimitStoreUnavailable(RuntimeError):
    """인증 시도 제한 저장소에 접근할 수 없을 때 발생한다."""


@dataclass(frozen=True)
class RateLimitDecision:
    """한 번의 인증 시도를 허용할지에 대한 판단."""

    allowed: bool
    retry_after_seconds: int


class AuthAttemptLimiter(Protocol):
    """인증 시도 횟수를 기록하는 저장소의 계약."""

    async def consume(self, client_identifier: str) -> RateLimitDecision:
        """현재 인증 시도를 기록하고 허용 여부를 반환한다."""

    async def reset(self, client_identifier: str) -> None:
        """인증 성공 후 해당 클라이언트의 실패 기록을 제거한다."""


class RedisAuthAttemptLimiter:
    """여러 API 인스턴스가 공유하는 Redis 기반 인증 시도 제한기."""

    _CONSUME_SCRIPT: Final = """
local attempts = redis.call('INCR', KEYS[1])
if attempts == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {attempts, ttl}
"""

    def __init__(self, redis: Redis, attempt_limit: int, window_seconds: int) -> None:
        self._redis = redis
        self._attempt_limit = attempt_limit
        self._window_seconds = window_seconds

    async def consume(self, client_identifier: str) -> RateLimitDecision:
        """고정된 시간 창 안에서 허용된 시도 횟수를 원자적으로 계산한다."""

        key = self._key_for(client_identifier)
        try:
            raw_result = await cast(
                Awaitable[object],
                self._redis.eval(
                    self._CONSUME_SCRIPT,
                    1,
                    key,
                    str(self._window_seconds),
                ),
            )
        except RedisError as error:
            raise RateLimitStoreUnavailable("Redis rate-limit check failed") from error

        attempts, remaining_seconds = cast(list[int], raw_result)
        retry_after_seconds = max(remaining_seconds, 1)
        return RateLimitDecision(
            allowed=attempts <= self._attempt_limit,
            retry_after_seconds=retry_after_seconds,
        )

    async def reset(self, client_identifier: str) -> None:
        """인증 성공 시 실패 횟수를 초기화한다."""

        try:
            await self._redis.delete(self._key_for(client_identifier))
        except RedisError as error:
            raise RateLimitStoreUnavailable("Redis rate-limit reset failed") from error

    @staticmethod
    def _key_for(client_identifier: str) -> str:
        identifier_hash = sha256(client_identifier.encode("utf-8")).hexdigest()
        return f"{RATE_LIMIT_KEY_PREFIX}{identifier_hash}"
