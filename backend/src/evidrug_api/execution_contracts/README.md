# Agent 및 Tool 실행 계약

이 디렉터리는 Agent의 과학적 판단과 orchestration의 기계적 실행 사이에 교환되는
versioned Pydantic 계약을 정의한다. DB table, queue 제어, 실제 모델 호출은 포함하지 않는다.

## 읽는 순서

1. `common.py`: 원본 결과 참조, 오류, 예산, 사용량과 버전
2. `tool.py`: 전문 Agent가 만드는 `ToolRequest`와 실행 계층의 `ToolObservation`
3. `agent.py`: 모든 Agent가 공유하는 `AgentInput`과 `AgentOutput`

## Agent 호출 번호 규칙

`AgentInput.attempt`는 분석 전체의 전역 번호가 아니라 **같은 Agent 안의 호출 번호**다.
첫 호출은 1번이며 독립적인 `run_id`를 가진다. 같은 Agent를 다시 호출할 때는 다음 번호와
새 run ID를 사용하고 이전 호출의 원본을 덮어쓰지 않는다. 현재 실행기는 ADMET·Decision의
2번 호출만 허용한다. Decision 1번은 추가 근거 요청을 한 건만 생성할 수 있으므로 그 run ID를
요청 상관 ID로 사용한다. ADMET 2번 `AgentInput.recall_request`는 그 요청 내용을 보존하고,
Decision 2번 `AgentInput.recall_feedback.run_id`는 응답한 ADMET 2번을 가리킨다. 두 필드는
1번 호출에 넣을 수 없다. `parent_run_id`는 저장 계층의 연결이며 개발용 replay의 원본
참조에도 쓰이므로, 요청·응답 의미는 검증된 입력과 함께 확인해야 한다.

## 책임 경계

- 전문 Agent는 조사 목적과 도구를 선택하고, 결과를 근거·공백·경고로 해석한다.
- Decision Agent는 특정 도구가 아니라 추가로 필요한 조사 목적을 `FollowupRequest`로 만든다.
- orchestration은 입력 schema, 등록 도구, 권한, 예산과 중복을 검사하고 승인·거부를 기록한다.
- 원본 예측 결과는 `ArtifactReference`로 가리킨다. 큰 JSON을 Agent 사이에서 반복 전달하지 않는다.

```text
Decision FollowupRequest
        ↓
전문 Agent → ToolRequest → 실행 계층 → ToolObservation
        ↓                         │
 EvidenceClaim/Gaps              └─ 승인·오류·사용량·원본 참조
        ↓
Decision AgentOutput
```

## Agent별 확장

`ToolRequest[ArgumentsModel]`, `ToolObservation[ResultModel]`, `AgentOutput[ResultModel]`처럼
각 기능의 Pydantic model을 generic payload로 지정한다. 공통 envelope에 임의 `dict`를 넣지 않는다.

```python
class AdmetArguments(BaseModel):
    canonical_smiles: str


request = ToolRequest[AdmetArguments](
    # 공통 식별자와 버전 생략
    arguments=AdmetArguments(canonical_smiles="CCO"),
    objective="기본 ADMET endpoint를 예측한다.",
)
```

`schema_version`이 바뀌는 호환성 파괴 변경은 기존 버전을 읽는 migration 또는 adapter와 함께
도입한다. 후속 DB 작업은 이 모델의 반복 필드를 정규화하되, API/worker 경계에서는 동일한
계약을 재구성해야 한다.

첫 실제 확장은 [`evidrug_api.admet`](../admet/README.md)에 있다. ADMET tool은
`AdmetToolArguments`와 `AdmetToolResult`를 공통 envelope의 generic payload로 사용하고,
모델·endpoint metadata는 실행마다 복사하지 않고 `AdmetModelManifest`로 분리한다.

실제 등록·권한·run 예산·공통 관측 변환은 [tool_admission](../tool_admission/README.md)에서
수행한다. Agent 루프와 전체 orchestration은 아직 연결하지 않았다.
