"""고정 JSON-lines protocol로 격리 CToxPred2 runtime을 호출한다."""

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import uuid4

from pydantic import Field

from evidrug_api.execution_contracts.common import ContractModel


class CtoxProviderUnavailable(RuntimeError):
    """격리 runtime을 시작하거나 검증된 응답을 받지 못한 경우."""

    code = "provider_unavailable"


class _RuntimeReply(ContractModel):
    protocol: Literal[1]
    request_id: str
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report: dict[str, object]


class CtoxPred2Provider:
    """인스턴스별 프로세스 하나를 직렬 재사용하고 취소 시 종료한다."""

    def __init__(self, command: Sequence[str]) -> None:
        if isinstance(command, str) or not command or any(not part for part in command):
            raise ValueError("runtime command must be a non-empty argument sequence")
        self.command = tuple(command)
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def predict(self, canonical_smiles: str) -> Mapping[str, object]:
        async with self._lock:
            if self._closed:
                raise CtoxProviderUnavailable("runtime provider is closed")
            try:
                return await self._exchange(canonical_smiles)
            except asyncio.CancelledError:
                await self._stop()
                raise
            except (OSError, ValueError, EOFError, asyncio.IncompleteReadError) as error:
                await self._stop()
                raise CtoxProviderUnavailable("CToxPred2 runtime unavailable") from error

    async def _exchange(self, canonical_smiles: str) -> Mapping[str, object]:
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
                    limit=65536,
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
        arguments = {"schema_version": "1", "canonical_smiles": canonical_smiles}
        encoded = json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        request = (
            json.dumps(
                {"protocol": 1, "request_id": request_id, "arguments": arguments},
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        if len(request) > 65536:
            raise ValueError("request exceeds runtime line limit")
        process.stdin.write(request)
        await process.stdin.drain()
        line = await process.stdout.readline()
        if not line or not line.endswith(b"\n"):
            raise EOFError("runtime ended without complete response")
        reply = _RuntimeReply.model_validate_json(line)
        if (
            reply.request_id != request_id
            or reply.arguments_sha256 != hashlib.sha256(encoded).hexdigest()
        ):
            raise ValueError("runtime response does not match request")
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
        async with self._lock:
            self._closed = True
            await self._stop()
