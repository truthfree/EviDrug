"""별도 Python 환경의 DeepPurpose를 JSON-lines로 호출하는 비동기 provider."""

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Sequence
from typing import Literal
from uuid import uuid4

from pydantic import Field

from evidrug_api.dta.adapter import DtaProviderUnavailable
from evidrug_api.dta.contracts import DtaArguments, DtaModel, DtaObservation, DtaScoreType, Sha256
from evidrug_api.execution_contracts.common import ContractModel

logger = logging.getLogger(__name__)
CNN_CNN_BINDINGDB_MODEL = DtaModel(
    provider="deeppurpose",
    model_id="CNN_CNN_BindingDB",
    version="0.1.5",
    artifact_sha256="1f5c62863303d5057566b3b29be24e31cc138b361dfb8b085b53c8169d8c6829",
)
MPNN_CNN_BINDINGDB_MODEL = DtaModel(
    provider="deeppurpose",
    model_id="MPNN_CNN_BindingDB",
    version="0.1.5",
    artifact_sha256="655119c3896a773a8ccf0e711ad263a3bfbcd0bab7ea5085cccb12e13908bc0c",
)
# 기존 import 호환을 유지한다.
DEEPPURPOSE_MODEL = CNN_CNN_BINDINGDB_MODEL


class RuntimeMetrics(ContractModel):
    cold_start: bool
    load_seconds: float = Field(ge=0, allow_inf_nan=False)
    inference_seconds: float = Field(ge=0, allow_inf_nan=False)
    peak_rss_mib: float = Field(gt=0, allow_inf_nan=False)


class RuntimeReply(ContractModel):
    protocol: Literal[1]
    request_id: str
    arguments_sha256: Sha256
    model: DtaModel
    observations: tuple[DtaObservation, ...] = Field(min_length=1, max_length=1)
    runtime: RuntimeMetrics


class DeepPurposeProvider:
    """인스턴스별 모델 프로세스 하나를 유지한다. 종료 시 aclose를 호출한다."""

    def __init__(
        self,
        command: Sequence[str],
        model: DtaModel = CNN_CNN_BINDINGDB_MODEL,
    ) -> None:
        if isinstance(command, str) or not command or any(not part for part in command):
            raise ValueError("runtime command must be a non-empty argument sequence")
        if model not in (CNN_CNN_BINDINGDB_MODEL, MPNN_CNN_BINDINGDB_MODEL):
            raise ValueError("unsupported DeepPurpose model")
        self.command = tuple(command)
        self.model = model
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def predict(self, arguments: DtaArguments) -> tuple[DtaObservation, ...]:
        """직렬 실행하며 다른 입력·모델의 응답과 잘못된 protocol을 거부한다."""
        async with self._lock:
            if self._closed:
                raise DtaProviderUnavailable("runtime provider is closed")
            try:
                return await self._exchange(arguments)
            except asyncio.CancelledError:
                await self._stop()
                raise
            except (OSError, ValueError, EOFError, asyncio.IncompleteReadError) as error:
                await self._stop()
                raise DtaProviderUnavailable("DeepPurpose runtime unavailable") from error

    async def _exchange(self, arguments: DtaArguments) -> tuple[DtaObservation, ...]:
        if self._process is None or self._process.returncode is not None:
            await self._stop()
            # 모델 프로세스에 API key를 상속하지 않는다. command는 관리자가 고정한다.
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
                    limit=65536,
                )
            )
            try:
                self._process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # 시작 중 취소되어도 PID를 확보해 predict의 종료 경로에서 회수한다.
                self._process = await spawn
                raise
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        request_id = uuid4().hex
        payload = arguments.model_dump(mode="json")
        expected_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        request = (
            json.dumps({"protocol": 1, "request_id": request_id, "arguments": payload}).encode()
            + b"\n"
        )
        if len(request) > 65536:
            raise ValueError("request exceeds runtime line limit")
        process.stdin.write(request)
        await process.stdin.drain()
        line = await process.stdout.readline()
        if not line or not line.endswith(b"\n"):
            raise EOFError("runtime ended without complete response")
        reply = RuntimeReply.model_validate_json(line)
        if (
            reply.request_id != request_id
            or reply.arguments_sha256 != expected_hash
            or reply.model != self.model
            or reply.observations[0].score_type != DtaScoreType.PREDICTED_PKD
        ):
            raise ValueError("runtime response does not match request or model")
        logger.info("DTA runtime completed", extra={"dta_runtime": reply.runtime.model_dump()})
        return reply.observations

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
        """진행 중 호출이 끝난 뒤 모델 프로세스를 해제한다."""
        async with self._lock:
            self._closed = True
            await self._stop()
