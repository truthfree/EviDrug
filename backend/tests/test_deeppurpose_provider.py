import asyncio
import sys

import pytest

from evidrug_api.dta.adapter import DtaProviderUnavailable, DtaToolAdapter
from evidrug_api.dta.contracts import DtaArguments
from evidrug_api.dta.deeppurpose import (
    DEEPPURPOSE_MODEL,
    MPNN_CNN_BINDINGDB_MODEL,
    DeepPurposeProvider,
)

ARGUMENTS = DtaArguments(canonical_smiles="CCO", target_sequence="ACDEFGHIK")

# 외부 모델 없이도 실제 pipe, 종료, timeout을 검증하는 작은 독립 프로세스.
PROGRAM = """
import hashlib, json, os, sys, time
mode = sys.argv[1]
model = json.loads(sys.argv[2])
for count, line in enumerate(sys.stdin, 1):
    request = json.loads(line)
    if mode == "hang":
        time.sleep(60)
    if mode == "crash":
        sys.exit(1)
    if mode == "long":
        print("x" * 70000, flush=True)
        continue
    payload = json.dumps(
        request["arguments"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    reply = {
        "protocol": 1, "request_id": request["request_id"],
        "arguments_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "model": model,
        "observations": [{
            "score_type": "predicted_pkd", "value": float(count), "unit": "-log10(Kd [M])"
        }],
        "runtime": {
            "cold_start": count == 1, "load_seconds": 0.1,
            "inference_seconds": 0.01, "peak_rss_mib": 10
        },
    }
    if mode == "wrong_id":
        reply["request_id"] = "wrong"
    if mode == "wrong_input":
        reply["arguments_sha256"] = "0" * 64
    if mode == "wrong_model":
        reply["model"]["version"] = "wrong"
    if mode == "wrong_score":
        reply["observations"][0].update(
            score_type="binding_probability", value=0.2, unit="probability"
        )
    if mode == "environment" and "EVIDRUG_OPENAI_API_KEY" in os.environ:
        sys.exit(1)
    print(json.dumps(reply), flush=True)
"""


def provider(mode: str = "success") -> DeepPurposeProvider:
    return DeepPurposeProvider(
        (sys.executable, "-u", "-c", PROGRAM, mode, DEEPPURPOSE_MODEL.model_dump_json())
    )


@pytest.mark.asyncio
async def test_reuses_process_and_serializes_concurrent_calls() -> None:
    runtime = provider()
    try:
        results = await asyncio.gather(*(runtime.predict(ARGUMENTS) for _ in range(3)))
        assert [result[0].value for result in results] == [1, 2, 3]
    finally:
        await runtime.aclose()
    with pytest.raises(DtaProviderUnavailable):
        await runtime.predict(ARGUMENTS)


@pytest.mark.asyncio
async def test_accepts_pinned_mpnn_cnn_runtime_identity() -> None:
    runtime = DeepPurposeProvider(
        (
            sys.executable,
            "-u",
            "-c",
            PROGRAM,
            "success",
            MPNN_CNN_BINDINGDB_MODEL.model_dump_json(),
        ),
        MPNN_CNN_BINDINGDB_MODEL,
    )
    try:
        observations = await runtime.predict(ARGUMENTS)
        assert observations[0].value == 1
        assert runtime.model.model_id == "MPNN_CNN_BindingDB"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["crash", "long", "wrong_id", "wrong_input", "wrong_model", "wrong_score"]
)
async def test_bad_response_is_unavailable_and_process_is_discarded(mode: str) -> None:
    runtime = provider(mode)
    try:
        result = await DtaToolAdapter(runtime).execute(ARGUMENTS)
        assert result.status == "unavailable"
        assert result.observations == ()
        assert runtime._process is None
        runtime.command = provider().command
        assert (await runtime.predict(ARGUMENTS))[0].value == 1
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_timeout_kills_child_then_allows_fresh_process() -> None:
    runtime = provider("hang")
    try:
        result = await DtaToolAdapter(runtime, timeout_seconds=0.1).execute(ARGUMENTS)
        assert result.error_code == "provider_timeout"
        assert runtime._process is None
        runtime.command = provider().command
        assert (await runtime.predict(ARGUMENTS))[0].value == 1
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_kill_active_call() -> None:
    runtime = provider("hang")
    active = asyncio.create_task(runtime.predict(ARGUMENTS))
    try:
        async with asyncio.timeout(5):
            while runtime._process is None:
                await asyncio.sleep(0.01)
        child = runtime._process
        waiting = asyncio.create_task(runtime.predict(ARGUMENTS))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert child.returncode is None
    finally:
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        await runtime.aclose()
    assert child.returncode is not None


@pytest.mark.asyncio
async def test_subprocess_does_not_inherit_api_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVIDRUG_OPENAI_API_KEY", "synthetic-placeholder")
    runtime = provider("environment")
    try:
        assert (await runtime.predict(ARGUMENTS))[0].value == 1
    finally:
        await runtime.aclose()
