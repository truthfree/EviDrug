"""사용자 노출 LLM 서술과 구조화 필드의 문체 경계를 검증한다."""

import hashlib

from evidrug_api.decision.agent import DECISION_PROMPT_VERSION
from evidrug_api.decision.agent import INSTRUCTIONS as DECISION
from evidrug_api.orchestration.reasoning import INSTRUCTIONS as SPECIALIST
from evidrug_api.target_hypothesis.reasoner import INSTRUCTIONS as TARGET
from evidrug_api.target_hypothesis.reasoner import PROMPT_VERSION


def test_target_prompt_requests_polite_reader_facing_rationales() -> None:
    assert PROMPT_VERSION == "target-causal-support-v7"
    assert "polite formal style" in TARGET
    assert "rationale and causal_rationale" in TARGET
    assert "at most two decimal places" in TARGET


def test_specialist_prompt_requests_polite_summary_and_limitations() -> None:
    assert "polite formal style" in SPECIALIST
    assert "summary and each limitation" in SPECIALIST
    assert "at most two decimal places" in SPECIALIST


def test_decision_prompt_requests_polite_prose_without_changing_codes() -> None:
    assert DECISION_PROMPT_VERSION == "decision-gated-synthesis-v5.7"
    assert hashlib.sha256(DECISION.encode()).hexdigest()[:12] == "d5bd73424fca"
    assert "All twelve output fields are required" in DECISION
    assert "Do not change verdict enums, citation IDs" in DECISION
