# 고정 DAG 기반 분석 orchestration MVP

## 배경과 범위

분석 작업과 ADMET·DTA 실행 기반은 준비됐지만 기존 worker는 분석 ID만 수신하고 단계 실행과
상태 전이를 수행하지 않았다. 실제 LLM Agent의 비결정성과 분리하여 workflow 정책을 검증할 수
있도록 fixed DAG, Agent 실행 저장과 장애 회수 기반을 먼저 구현한다.

포함 범위는 Agent executor 주입 경계, 분석 실행 lease, Agent run/output artifact,
append-only trace, 단계 상태 전이와 scripted executor 테스트다. 실제 Agent prompt, 자율 도구
선택, Decision recall, 자동 retry, evaluation replay, 프런트엔드와 실제 배포는 제외한다.

## 실행 흐름

1. Celery는 `analysis_id`만 전달한다.
2. worker가 분석 실행 소유권을 획득하고 `queued`를 `running`으로 바꾼다.
3. Target Hypothesis와 ADMET을 병렬 실행한다.
4. Target이 유효한 DTA 입력을 제공하면 DTA를 시작하고, 아니면 명시적으로 skip한다.
5. 전문 단계가 terminal에 도달하면 사용 가능한 output artifact만 Decision에 전달한다.
6. Decision 성공 후 모든 전문 단계가 정상이면 `completed`, 실패나 누락이 있으면
   `partial_failure`다.
7. 전문 근거가 전혀 없거나 Decision이 실패하면 분석은 `failed`다.

한 Agent 실패가 병렬 Agent를 취소하지 않으며, 이미 저장된 output을 삭제하지 않는다.
`skipped_due_to_missing_target`은 음성 DTA 예측이 아니고 근거 누락이다.

## 실행과 저장 계약

Agent는 공통 `AgentInput`을 받고 `AgentOutput`을 반환한다. orchestration은 식별자와 schema를
검증하고 전체 output JSON 및 SHA-256을 Agent run에 저장한다. downstream Agent에는 본문을
복사하지 않고 `ArtifactReference`를 전달한다. 실제 context builder가 필요한 내용을 읽는 연결은
후속 Agent adapter 범위다.

실행 시작 전에 profile snapshot/hash와 owner/lease를 commit한다. 동일 analysis 재전달은
실행 중이면 중복 호출을 거부하고 terminal이면 기존 상태를 반환한다. lease가 만료되면 늦은
결과 저장을 거부하고 running 단계를 실패, pending 단계를 skip하며 전체 분석을 실패시킨다.

기본 worker는 Dacon 키가 구성되면 Target Hypothesis adapter를 등록한다.
PR #116 이후 [PoC opt-in 구성](poc-full-execution.md)은 ADMET·DTA·Decision도 등록한다.
기본 구성에서 미등록된 단계 또는 필요한 키가 없는 환경은 `agent_not_configured`를 기록한다.
성공을 흉내 내거나 실제 도구를 registry 밖에서 직접 호출하지 않는다.

## 완료 조건과 검증 한계

- Target과 ADMET의 병렬 시작 및 Target 직후 DTA 시작을 검증한다.
- target 입력 누락, 전문 단계별 실패, 근거 없음과 Decision 실패 상태를 검증한다.
- 중복 Celery 전달이 Agent run을 중복 생성하지 않는다.
- 현재 상태와 terminal trace가 함께 저장되고 lease 만료가 영구 running을 남기지 않는다.
- 기존 백엔드 회귀 검사와 PostgreSQL migration SQL 생성이 통과한다.

SQLite 테스트는 PostgreSQL의 실제 조건부 갱신·잠금 부하를 대체하지 않는다. worker 강제 종료,
Beat 회수와 실제 Agent/provider를 포함한 검증은 운영 연결 Issue에서 수행한다.

관련 백엔드 [Issue #83](https://github.com/truthfree/EviDrug-Dacon2026/issues/83).
