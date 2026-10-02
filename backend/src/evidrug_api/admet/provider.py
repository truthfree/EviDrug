"""별도 ADMET Python 환경을 재사용하는 subprocess provider."""

import asyncio
import hashlib
import json
import logging
import math
import os
from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import uuid4

from pydantic import Field

from evidrug_api.execution_contracts.common import ContractModel

logger = logging.getLogger(__name__)


class AdmetProviderUnavailable(RuntimeError):
    """실행 실패를 예측 결과와 구분하는 공개 가능한 오류."""

    def __init__(self, code: Literal["provider_timeout", "provider_unavailable"]) -> None:
        self.code = code
        super().__init__(code)


class AdmetRuntimeMetrics(ContractModel):
    cold_start: bool
    load_seconds: float = Field(ge=0, allow_inf_nan=False)
    inference_seconds: float = Field(ge=0, allow_inf_nan=False)
    peak_rss_mib: float = Field(gt=0, allow_inf_nan=False)


class _Reply(ContractModel):
    protocol: Literal[1]
    request_id: str
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report: dict[str, object]
    runtime: AdmetRuntimeMetrics


class AdmetSubprocessProvider:
    """동일 event loop에서 재사용하며 종료 시 aclose로 프로세스를 회수한다."""

    def __init__(self, command: Sequence[str], *, timeout_seconds: float = 300) -> None:
        if isinstance(command, str) or not command or any(not part for part in command):
            raise ValueError("runtime command must be a non-empty argv")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout must be positive and finite")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def predict(self, canonical_smiles: str) -> Mapping[str, object]:
        """대기·실행을 합친 timeout을 적용하고 실패 시 가짜 report를 반환하지 않는다."""
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self._lock:
                    if self._closed:
                        raise AdmetProviderUnavailable("provider_unavailable")
                    try:
                        return await self._exchange(canonical_smiles)
                    except asyncio.CancelledError:
                        await self._stop()
                        raise
                    except (OSError, ValueError, EOFError) as error:
                        await self._stop()
                        raise AdmetProviderUnavailable("provider_unavailable") from error
        except TimeoutError as error:
            raise AdmetProviderUnavailable("provider_timeout") from error

    async def _exchange(self, smiles: str) -> Mapping[str, object]:
        if self._process is None or self._process.returncode is not None:
            await self._stop()
            environment = {
                key: os.environ[key]
                for key in ("PATH", "HOME", "LANG", "TMPDIR")
                if key in os.environ
            }
            environment.update(
                OMP_NUM_THREADS="1",
                MKL_NUM_THREADS="1",
                CUDA_VISIBLE_DEVICES="",
                PYTHONUNBUFFERED="1",
            )
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *self.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=environment,
                    limit=2 * 1024 * 1024,
                )
            )
            try:
                self._process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                self._process = await spawn
                raise
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        request_id = uuid4().hex
        line = (
            json.dumps({"protocol": 1, "request_id": request_id, "smiles": smiles}).encode() + b"\n"
        )
        if len(line) > 32768:
            raise ValueError("request too large")
        process.stdin.write(line)
        await process.stdin.drain()
        response = await process.stdout.readline()
        if not response or not response.endswith(b"\n"):
            raise EOFError("runtime stopped without a complete reply")
        reply = _Reply.model_validate_json(response)
        if (
            reply.request_id != request_id
            or reply.input_sha256 != hashlib.sha256(smiles.encode()).hexdigest()
        ):
            raise ValueError("runtime reply does not match input")
        packages = reply.report.get("packages")
        if not isinstance(packages, dict) or packages.get("admet-ai") != "1.4.0":
            raise ValueError("unexpected runtime version")
        if reply.report.get("smiles") != smiles:
            raise ValueError("report does not match input")
        logger.info("ADMET runtime completed", extra={"admet_runtime": reply.runtime.model_dump()})
        return reply.report

    async def _stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()

    async def aclose(self) -> None:
        """현재 호출 종료 후 모델 프로세스를 닫고 신규 호출을 거부한다."""
        async with self._lock:
            self._closed = True
            await self._stop()
