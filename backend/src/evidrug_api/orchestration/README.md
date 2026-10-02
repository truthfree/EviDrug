# 분석 orchestration

## 목적과 책임

한 분석에서 Target Hypothesis와 ADMET을 병렬로 시작하고, Target 결과가 DTA 입력을
제공할 때만 DTA를 실행한 뒤 사용 가능한 전문 결과로 Decision을 실행한다. Agent는 과학적
결과를 만들지만 전체 분석 상태를 직접 변경하지 않는다. orchestration은 실행 순서, 현재 상태,
실패 전파, 중복 방지와 append-only trace만 소유한다.

고정 DAG는 `AgentExecutor` 주입 경계와 scripted test double로 workflow를 검증한다. 실제
기본 worker는 Dacon 키가 있을 때 Target handler만 구성한다. 전용 PoC worker에서는
`EVIDRUG_POC_MODELS_ENABLED=true`로 ADMET/DTA/Decision도 연결한다. 키가 없거나 기본 환경의
미구성 단계는 `agent_not_configured`로 기록하며 가짜 성공을 만들지 않는다.
[전체 PoC 실행 안내](../../../../docs/features/poc-full-execution.md)에서 배포와 검증 범위를 확인한다.

## 구조와 진입점

```text
orchestration/
├── contracts.py    실행 profile, executor 계약과 trace event
├── executor.py     단계별 실제 Agent handler 분배
├── tables.py       분석 lease, Agent run과 execution trace
├── repository.py   소유권 claim, 원자 상태 전이와 만료 회수
├── service.py      고정 DAG와 최종 분석 상태 결정
├── runtime.py      Celery용 독립 engine 실행 진입점
├── replay_cli.py   개발용 단독 실행 prepare/run/show 명령
├── replay_store.py 저장 입력·출처·schema 검증과 불변 실행 계획
├── replay.py       선택 Agent 실행 또는 완료 출력의 명시적 재사용
└── recover.py      만료 분석 회수 진입점
```

| 이름 | 위치 | 역할 | 입력·출력 | 오류 |
| :--- | :--- | :--- | :--- | :--- |
| `AnalysisOrchestrator.run` | `service.py` | fixed DAG 실행 | analysis ID → terminal 상태 | 실행 중복, lease 상실, DB 오류 |
| `AgentExecutor.execute` | `contracts.py` | Agent 교체 경계 | `AgentInput` → `AgentOutput`과 DTA 준비 여부 | Agent별 typed 오류 |
| `OrchestrationRepository` | `repository.py` | 상태·run·trace 원자 저장 | 소유권과 단계 결과 | 잘못된 상태, lease 상실 |
| `recover_expired_analyses` | `recover.py` | 만료 실행 회수 | database URL → 회수 수 | DB 연결 오류 |

## 고정 DAG와 상태 정책

1. `queued` 분석을 조건부 claim하여 `running`으로 commit한다.
2. 서로 다른 DB session에서 Target Hypothesis와 ADMET을 병렬 시작한다.
3. Target이 DTA 입력을 제공하면 ADMET 종료를 기다리지 않고 DTA를 시작한다.
4. Target 입력이 없으면 DTA를 `skipped_due_to_missing_target`으로 종료한다.
5. 전문 단계가 모두 terminal이면 성공 또는 부분 성공 output reference만 Decision에 전달한다.
6. 전문 근거가 하나도 없으면 Decision을 `insufficient_evidence`로 skip하고 분석을 실패시킨다.
7. Decision이 성공하면 전문 단계 누락·실패 여부에 따라 `completed` 또는
   `partial_failure`, Decision이 실패하면 전체를 `failed`로 종료한다.

`decision_phase`, 최종 `verdict`와 분석 상태는 서로 다른 상태 공간이다. orchestration은
Agent의 판정이나 rationale을 수정하지 않는다. Agent의 `partial_failure`는 Agent run과 trace에
보존하고 현재 단계 표시는 기존 공개 계약에 맞춰 `completed`로 둔다.

## 저장과 중복 방지

- `analysis_executions`: 분석당 하나의 owner token, profile snapshot/hash와 lease
- `agent_runs`: Agent 입력 JSON/hash, versioned output JSON/hash, 사용량과 오류
- `agent_replays`: 개발용 단독 실행의 원본 참조·upstream fingerprint·버전·원본 비용
- `execution_trace_events`: 시작·완료·실패·skip·lease 만료 append-only event
- `analysis_stages`, `analyses`: polling API가 반환하는 현재 상태
- `analysis_events`: 사용자에게 공개 가능한 상위 분석 상태 이력

동일 analysis의 terminal 재전달은 저장된 상태를 반환하며 Agent를 다시 호출하지 않는다.
유효한 실행 중 재전달은 `OrchestrationInProgress`다. 강제 종료 후 lease가 만료되면 Beat가
running Agent run을 실패, 남은 pending 단계를 skip, 분석을 `failed`로 만든다. 자동 재실행은
하지 않는다.

기본 lease는 1,200초이고 단계 timeout은 300초다. heartbeat는 아직 없으므로 실제 Agent 실행
시간을 늘릴 때는 전체 critical path보다 충분히 큰 lease를 설정하거나 heartbeat를 먼저
도입해야 한다. 환경당 Beat는 하나만 운영한다.

## 확장 지점과 제외 범위

- 실제 Target/ADMET/DTA/Decision adapter는 `AgentExecutor` 뒤에 연결한다.
- Target adapter는 [Target Prioritization Agent](../target_hypothesis/README.md)에 연결됐으며,
  typed shortlist에서 하나 이상의 UniProt ID와 단백질 서열을 검증한 뒤에만
  `provides_dta_input=True`를 반환한다. 현재 orchestration은 DTA stage를 한 번 시작하고,
  shortlist fan-out은 PoC DTA Agent가 stage 내부에서 공통 tool ledger를 사용해 순차 수행한다.
- 전문 Agent 내부 도구 호출은 기존 ToolRegistry/admission/runner를 사용한다.
- Decision cardiac evidence-gap recall은 실행 profile의 `max_recall_depth=1`일 때 한 번만
  허용한다. 첫 Decision은 도구명 없이 추가 근거를 요청하며, Orchestrator가 ADMET attempt 2에
  전달한다. ADMET는 capability matcher에서 CToxPred2를 선택하고 기존 admission으로 실행한다.
  성공 또는 실패 결과를 받은 Decision attempt 2가 최종 판단을 남긴다. 각 attempt의 parent run,
  후보 제외 사유, admission/관측 참조, 상태 전이는 trajectory episode에 기록한다. 현재
  cardiac ADMET recall만 구현됐고 일반 FollowupRequest의 동적 라우팅은 후속 범위다.
  주입된 CToxPred2 snapshot을 사용하는 평가 경로는 같은 typed 관측을 반환하며
  `replay_miss`에서도 live provider로 전환하지 않는다.
- Agent 호출 번호는 분석 내 **Agent별** `attempt`(공개 API의 `call_number`)이다. 모든 Agent의
  첫 실행은 1번이며 서로 다른 Agent의 번호를 하나의 전역 순서로 해석하지 않는다. 같은 Agent의
  후속 실행은 이전 번호를 건너뛰지 않고 새 run ID를 사용한다. 현재 실행 정책은 ADMET·Decision
  2번까지만 허용한다. Decision 1번은 한 건의 추가 근거 요청만 발행할 수 있으므로 그 run ID가
  요청의 상관 ID다. ADMET 2번은 입력에 요청 내용과 Decision 1번 parent run ID를 보존하고,
  Decision 2번은 입력 feedback에 ADMET 2번 응답 run ID를 보존한다. 결과 조회 `calls`는 이
  저장 관계와 해시를 검증한 뒤 요청→응답을 연결한다. `cardiac`은 호출 번호가 아니라 2번
  호출의 결과 종류다. 다른 follow-up이나 3번 호출을 허용할 때는 실행 정책·typed 결과·예산을
  명시적으로 확장하며 기존 의미 슬롯을 추가하지 않는다.
- 일반 경량 runtime은 recall depth 0을 유지한다. 검증된 모델 자산을 포함하는 PoC Compose와
  staging worker는 `EVIDRUG_CTOXPRED2_RECALL_ENABLED=true`를 기본 주입해 이 경로를
  활성화한다. `/opt/ctoxpred2/.venv/bin/python`, `/opt/ctoxpred2/runtime.py`, 검증된
  `/opt/ctoxpred2/artifacts`가 worker에 설치되어야 하며, 시작 시 자산 검증에 실패하면 실행을
  거부한다. 운영 rollback은 환경변수를 `false`로 명시해 recall depth 0으로 되돌린다.
- 명시적 단독 실행은 [Agent replay 사용법](../../../../docs/features/agent-replay.md)을 따른다.
  기존 lifecycle을 사용하는 `run_single`은 현재 Target만 제공한다.
  #110의 `continuation.py`와 `run_continuation`은 확정 계획의 완료 Target/ADMET만 참조해
  DTA/Decision을 실행한다. prepare/show는 무료이고 run은 `--allow-live`가 필요하다.
  일반 분석의 cross-analysis 차단을 유지하며 manifest의 두 원본만 예외로 허용한다.
  원본 비용은 reused_runs, 새 비용은 new_runs로 구분하고 DTA 전체 실패 시 Decision을 생략한다.
- 자동 기술 재시도, Target 근거 snapshot 기반 LLM-only replay(#106), evaluation suite와 동적
  profile 선택은 후속 범위다.

## 검증

```sh
uv run --no-env-file pytest tests/test_orchestration.py
uv run alembic upgrade head --sql
```

테스트는 전체 성공, Target 입력 누락, Target/ADMET/DTA 실패, 근거 없음, Decision 실패,
중복·동시 전달과 lease 만료를 SQLite와 scripted executor로 검증한다. 실제 PostgreSQL 잠금,
Celery worker 강제 종료와 실제 Agent/provider 연결은 통합 환경에서 별도로 확인해야 한다.

관련: [기능 명세](../../../../docs/features/analysis-orchestration.md),
[평가 가능 아키텍처](../../../../docs/decisions/002-evaluation-ready-agent-architecture.md),
Issue #83.
