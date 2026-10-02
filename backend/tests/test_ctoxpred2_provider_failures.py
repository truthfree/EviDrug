import asyncio
import sys
from textwrap import dedent

import pytest

from evidrug_api.ctoxpred2.provider import CtoxPred2Provider, CtoxProviderUnavailable
from evidrug_api.tool_execution.runner import failure_code


@pytest.mark.asyncio
async def test_missing_runtime_reports_provider_unavailable_without_fallback() -> None:
    provider = CtoxPred2Provider(("/nonexistent/ctoxpred2/python",))
    try:
        with pytest.raises(CtoxProviderUnavailable) as caught:
            await provider.predict("CCO")
        assert failure_code(caught.value) == "provider_unavailable"
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_runtime_exit_reports_provider_unavailable_without_retry() -> None:
    provider = CtoxPred2Provider((sys.executable, "-c", "raise SystemExit(7)"))
    try:
        with pytest.raises(CtoxProviderUnavailable) as caught:
            await provider.predict("CCO")
        assert failure_code(caught.value) == "provider_unavailable"
        assert provider._process is None
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_timeout_terminates_runtime_process() -> None:
    provider = CtoxPred2Provider((sys.executable, "-c", "import time; time.sleep(60)"))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(provider.predict("CCO"), timeout=0.2)
        assert provider._process is None
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_runtime_restarts_only_on_next_explicit_call() -> None:
    one_shot = dedent(
        """
        import hashlib
        import json
        import sys

        request = json.loads(sys.stdin.readline())
        encoded = json.dumps(
            request["arguments"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        print(json.dumps({
            "protocol": 1,
            "request_id": request["request_id"],
            "arguments_sha256": hashlib.sha256(encoded).hexdigest(),
            "report": {"case": request["arguments"]["canonical_smiles"]},
        }), flush=True)
        """
    )
    provider = CtoxPred2Provider((sys.executable, "-c", one_shot))
    try:
        assert await provider.predict("CCO") == {"case": "CCO"}
        assert provider._process is not None
        await provider._process.wait()
        assert await provider.predict("CCC") == {"case": "CCC"}
    finally:
        await provider.aclose()
