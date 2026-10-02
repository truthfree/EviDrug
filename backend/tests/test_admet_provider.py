import asyncio
import sys

import pytest

from evidrug_api.admet.provider import AdmetProviderUnavailable, AdmetSubprocessProvider

PROGRAM = """
import hashlib, json, os, sys, time
mode = sys.argv[1]
for count, line in enumerate(sys.stdin, 1):
    request = json.loads(line)
    if mode == "hang":
        time.sleep(60)
    if mode == "crash":
        sys.exit(1)
    if mode == "long":
        print("x" * 2200000, flush=True)
        continue
    report = {"smiles": request["smiles"], "packages": {"admet-ai": "1.4.0"}, "count": count}
    reply = {
        "protocol": 1, "request_id": request["request_id"],
        "input_sha256": hashlib.sha256(request["smiles"].encode()).hexdigest(),
        "report": report,
        "runtime": {"cold_start": count == 1, "load_seconds": 0.1,
                    "inference_seconds": 0.01, "peak_rss_mib": 10},
    }
    if mode == "wrong_id":
        reply["request_id"] = "wrong"
    if mode == "wrong_input":
        reply["input_sha256"] = "0" * 64
    if mode == "wrong_version":
        report["packages"]["admet-ai"] = "0"
    if mode == "environment" and "EVIDRUG_OPENAI_API_KEY" in os.environ:
        sys.exit(1)
    print(json.dumps(reply), flush=True)
"""


def provider(mode: str = "success", timeout: float = 5) -> AdmetSubprocessProvider:
    return AdmetSubprocessProvider(
        (sys.executable, "-u", "-c", PROGRAM, mode), timeout_seconds=timeout
    )


@pytest.mark.asyncio
async def test_reuses_process_for_serialized_concurrent_calls() -> None:
    runtime = provider()
    try:
        results = await asyncio.gather(*(runtime.predict("CCO") for _ in range(3)))
        assert [result["count"] for result in results] == [1, 2, 3]
    finally:
        await runtime.aclose()
    with pytest.raises(AdmetProviderUnavailable):
        await runtime.predict("CCO")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["crash", "long", "wrong_id", "wrong_input", "wrong_version"])
async def test_bad_runtime_is_discarded_and_next_call_restarts(mode: str) -> None:
    runtime = provider(mode)
    try:
        with pytest.raises(AdmetProviderUnavailable) as caught:
            await runtime.predict("CCO")
        assert caught.value.code == "provider_unavailable"
        assert runtime._process is None
        runtime.command = provider().command
        assert (await runtime.predict("CCO"))["count"] == 1
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_timeout_reaps_process_and_allows_retry() -> None:
    runtime = provider("hang", timeout=0.1)
    try:
        with pytest.raises(AdmetProviderUnavailable) as caught:
            await runtime.predict("CCO")
        assert caught.value.code == "provider_timeout"
        assert runtime._process is None
        runtime.command = provider().command
        runtime.timeout_seconds = 5
        assert (await runtime.predict("CCO"))["count"] == 1
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_kill_active_process() -> None:
    runtime = provider("hang")
    active = asyncio.create_task(runtime.predict("CCO"))
    try:
        async with asyncio.timeout(5):
            while runtime._process is None:
                await asyncio.sleep(0.01)
        child = runtime._process
        waiting = asyncio.create_task(runtime.predict("CCO"))
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
async def test_api_credentials_are_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVIDRUG_OPENAI_API_KEY", "synthetic-placeholder")
    runtime = provider("environment")
    try:
        assert (await runtime.predict("CCO"))["count"] == 1
    finally:
        await runtime.aclose()
