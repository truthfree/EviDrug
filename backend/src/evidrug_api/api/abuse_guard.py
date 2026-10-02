"""단일 프로세스 공개 테스트 API의 메모리·동시 작업·호출량 상한."""

import asyncio
import math
import time
from collections.abc import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from evidrug_api.config import Settings


class ApiAbuseGuard:
    """프록시 IP나 쿠키를 신뢰하지 않고 모든 API 요청에 공유 상한을 적용한다.

    카운터는 단일 이벤트 루프에서 await 없이 갱신한다. 재시작하면 초기화되며,
    다중 worker/인스턴스에 공유되지 않으므로 영속 비용 예산으로 사용하지 않는다.
    """

    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.settings = settings
        self.clock = clock
        self.window_started = clock()
        self.counts: dict[str, int] = {}
        self.active = 0
        prefix = settings.api_v1_prefix
        self.route_limits = {
            ("POST", f"{prefix}/auth/session"): settings.api_login_limit,
            ("POST", f"{prefix}/diseases/search"): settings.api_search_limit,
            ("POST", f"{prefix}/analysis-inputs/validate"): settings.api_validation_limit,
            ("POST", f"{prefix}/analyses"): settings.api_analysis_limit,
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """허용된 작은 요청만 앱에 전달하고, 완료/예외/취소 시 슬롯을 반환한다."""

        prefix = self.settings.api_v1_prefix
        if scope["type"] != "http" or not (
            scope["path"] == prefix or scope["path"].startswith(f"{prefix}/")
        ):
            await self.app(scope, receive, send)
            return
        # Render health probe는 작업 한도와 분리한다. 본문을 요구하지 않는다.
        if scope["method"] in {"GET", "HEAD"} and scope["path"] == f"{prefix}/health":
            await self.app(scope, receive, send)
            return
        retry_after = self._consume(scope)
        if retry_after:
            await self._reject(scope, receive, send, 429, "api_rate_limited", retry_after)
            return
        if self.active >= self.settings.api_max_concurrent_requests:
            await self._reject(scope, receive, send, 503, "api_busy", 1)
            return
        self.active += 1
        try:
            body = await self._read_body(scope, receive, send)
            if body is None:
                return
            delivered = False

            async def replay() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay, send)
        finally:
            self.active -= 1

    def _consume(self, scope: Scope) -> int:
        now = self.clock()
        window = self.settings.api_rate_window_seconds
        if now - self.window_started >= window:
            self.window_started = now
            self.counts.clear()
        retry_after = max(1, math.ceil(window - (now - self.window_started)))
        if self.counts.get("all", 0) >= self.settings.api_total_limit:
            return retry_after
        self.counts["all"] = self.counts.get("all", 0) + 1
        # 끝 슬래시도 같은 카운터에 포함해 리다이렉트 전 우회를 막는다.
        path = scope["path"].rstrip("/")
        limit = self.route_limits.get((scope["method"], path))
        if limit is not None:
            if self.counts.get(path, 0) >= limit:
                return retry_after
            self.counts[path] = self.counts.get(path, 0) + 1
        return 0

    async def _read_body(self, scope: Scope, receive: Receive, send: Send) -> bytes | None:
        chunks: list[bytes] = []
        size = 0
        try:
            async with asyncio.timeout(self.settings.api_body_timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return None
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > self.settings.api_max_body_bytes:
                        await self._reject(scope, receive, send, 413, "request_too_large")
                        return None
                    if chunk:
                        chunks.append(chunk)
                    if not message.get("more_body", False):
                        return b"".join(chunks)
        except TimeoutError:
            await self._reject(scope, receive, send, 408, "request_timeout")
            return None

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        status: int,
        code: str,
        retry_after: int | None = None,
    ) -> None:
        headers = {"Cache-Control": "no-store"}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        response = JSONResponse(
            status_code=status,
            content={"detail": {"code": code, "message": "Request limit reached. Try later."}},
            headers=headers,
        )
        await response(scope, receive, send)
