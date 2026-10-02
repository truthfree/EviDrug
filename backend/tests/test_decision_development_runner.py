"""유료 검증 runner의 호출 상한·실패 기록을 네트워크 없이 검사한다."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from decision_v57_support import case_fixture


def load_runner() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "check_decision_v57.py"
    spec = importlib.util.spec_from_file_location("development_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_call", [None, 2])
async def test_runner_makes_six_calls_without_retry_and_preserves_failed_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failed_call: int | None
) -> None:
    runner = load_runner()
    calls: list[tuple[str, str]] = []

    class FakeSettings:
        model_config: dict[str, object] = {}

        def __init__(self, *, openai_max_retries: int) -> None:
            assert openai_max_retries == 0
            assert self.model_config["env_file"] is None
            self.openai_api_key = object()
            self.openai_model = "gpt-5.6-sol"

    class FakeClient:
        closed = False

        async def generate_text(
            self, prompt: str, *, instructions: str, max_output_tokens: int
        ) -> SimpleNamespace:
            assert max_output_tokens == 4096
            calls.append((prompt, instructions))
            if len(calls) == failed_call:
                raise TimeoutError("synthetic timeout")
            _, output = case_fixture(1 if len(calls) <= 3 else 2)
            return SimpleNamespace(
                model="gpt-5.6-sol",
                status="completed",
                incomplete_reason=None,
                refused=False,
                usage=None,
                text=json.dumps(output),
            )

        async def close(self) -> None:
            self.closed = True

    client = FakeClient()
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(runner, "Settings", FakeSettings)
    monkeypatch.setattr(runner, "build_dacon_openai_client", lambda _: client)
    monkeypatch.setattr("sys.argv", ["runner", "--live", "--report", str(report_path)])
    await runner.main()
    report = json.loads(report_path.read_text())
    assert client.closed and len(calls) == len(report["runs"]) == 6
    assert calls[0] == calls[1] == calls[2]
    assert calls[3] == calls[4] == calls[5]
    assert [r["attempt"] for r in report["runs"]] == [1, 2, 3, 1, 2, 3]
    assert sum(r["status"] == "accepted" for r in report["runs"]) == (
        6 if failed_call is None else 5
    )
    if failed_call:
        assert report["runs"][failed_call - 1]["status"] == "model_error"
    with pytest.raises(SystemExit):
        await runner.main()
    assert len(calls) == 6
