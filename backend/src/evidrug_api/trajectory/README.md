# Trajectory assets

## 목적과 책임

이 모듈은 Agent가 행동을 선택한 결과만이 아니라 선택 당시의 상태, 가능한 행동 전체,
선택 뒤 관측과 상태 변화를 학습 가능한 append-only 자산으로 보존한다. 기존
`agent_runs`, `tool_admissions`, `tool_executions`를 실행 원장으로 유지하고 trajectory는
원 결과를 복제하지 않는 평가·학습용 projection이다.

현재 범위는 계약과 SQL 저장 계층, ADMET·CToxPred2 관측 snapshot/replay executor,
capability 기반 행동 후보 생성, 저장 episode의 구조 validator와 세 정책의 파일럿
replay 비교 runner다. 결정적 adaptive selector와 동일 snapshot의 네 번째 replay episode,
저장 선택의 재계산 validator도 연결됐다. 대량 생성 runner와 API/UI는 아직 연결하지 않았다.
일반 고정 DAG는 이
모듈을 사용하지 않으며, 선택적으로 활성화한
Decision cardiac recall만 trajectory episode를 추가한다.

`comparison_contracts.py`는 동일 분석의 ADMET·CToxPred2 snapshot을 고정한 세 정책
(`baseline`, `all_tools`, `decision_recall`) 비교 입력과 안정적인 실행 순서를 정의한다.
`validator.py:validate_stored_episode`는 SQL 읽기 전용으로 profile/step 해시, 연속 순서,
step 식별자, 상태·예산 전이, 실행 run 참조, live admission/실행 원장과 replay 도구 호출
사용량 0을 검사하고
고정 reason code와 첫 실패 step 번호를 반환한다. 이 validator 자체는 snapshot 원본의
hash/model version을 자체 검증하지 않는다. adaptive 선택 입력이 고정된 episode에
대해서는 selector 후보와 선택을 재계산한다. 정책 비교 runner는
별도로 snapshot 원본을 다시 검증한 뒤 각 typed replay executor를 호출한다.
따라서 일반 episode의 구조 검증 통과만으로 과학적 결과의 출처가 모두 검증됐다고
주장하지 않는다.

Decision cardiac recall이 활성화되면 요청, ADMET 도구 선택, Decision 재판단을 3개 step의
append-only episode로 저장한다. 각 step의 `source_run_id`와 `triggering_run_id`가 실행
관계를 잇는다. ADMET 2번 step은 요청한 Decision 1번을, 재판단 step은 응답한 ADMET 2번을
가리킨다. step에는 선택 가능한 행동과 제외 capability reason, 원
tool call/run 참조를 남긴다. 일반 고정 DAG에서 recall이 발생하지 않으면 episode를 만들지 않는다.

## 코드 읽는 순서

1. `contracts.py`: profile, state, action, step, evaluation, preference 계약
2. `tables.py`: episode와 append-only 자산 테이블
3. `repository.py`: episode 수명주기와 연속 step 저장
4. `admet_snapshot.py`: 원 도구 관측 snapshot과 replay miss가 닫힌 executor
5. `comparison_contracts.py`, `policy_comparison.py`, `validator.py`: 정책 비교 입력·실행과 구조 검사
6. `tests/test_trajectory.py`, `tests/test_trajectory_validator.py`: 사용 예시
7. `migrations/versions/0008_trajectory_assets.py`, `0009_admet_observation_snapshots.py`
8. `tool_admission/capabilities.py`: evidence gap과 결정적 행동 후보 생성

## 저장 흐름

```text
analysis
  └─ trajectory episode (고정 profile, live/replay)
       ├─ step 1 (state → candidates → selection → observations → state)
       ├─ step 2 ...
       ├─ independent evaluations
       └─ same-analysis episode preference pairs
```

- step은 1부터 빈틈없이 추가하며 종료된 episode는 수정하지 않는다.
- `available_actions`에 선택 당시 실제 후보 전체를 저장한다.
- 성공 관측은 tool call, Agent run 또는 artifact를 참조한다.
- replay episode는 시작 전에 observation snapshot version을 고정한다.
- evaluation은 생성 기록을 덮어쓰지 않고 evaluator별 독립 레코드로 추가한다.
- preference는 동일 analysis에서 나온 종료 episode끼리만 허용한다.
- ADMET snapshot은 원 tool call의 입력·결과·모델·데이터 hash를 고정한다.
- replay miss와 입력·버전 불일치는 실패하며 live provider로 전환하지 않는다.
- replay의 token, tool call과 external request 사용량은 0으로 별도 기록한다.

## 세 정책 replay 비교 파일럿 (#155)

`PolicyComparisonRunner(session).run(manifest)`는 한 analysis와 ADMET·CToxPred2
snapshot 각각 하나의 관측을 요구한다. 원 실행의 input/result hash 및 tool/model/data
identity를 재검증한 다음 `baseline`(ADMET), `all_tools`(ADMET+CToxPred2),
`decision_recall`(ADMET → Decision의 cardiac gap 요청 → CToxPred2)을 고정 순서로
실행한다. 이때 실제 provider나 Decision LLM을 호출하지 않는다. 각 호출은 기존 typed
replay executor를 통과하며, 성공·실패·`replay_miss`를 episode step에 남긴다.
replay의 Decision 1·2번 입력은 `PolicyDecisionInput`에 관측 참조·근거 키·남은 gap으로
기록한다. Decision 요청 step → ADMET 2번 응답 step → Decision 2번 재검토 step은
`triggering_step_id`로 이어지고 validator가 선행 step 및 Agent 방향을 확인한다.
실제 Agent run을 새로 만들지 않으므로 replay step의 `agent_call_number`는 시뮬레이션된
호출 번호이며 live run ID가 아니다.

보고서는 정책별 선택 도구, 성공/실패/miss, unresolved cardiac gap 수, recall 수,
replay 처리 시간과 구조 validator 판정을 반환한다. replay 시간은 live 추론 지연 시간이
아니다. `live_tool_calls`와 `external_requests`는 0이다. `aggregate_reports`는
여러 case 보고서의 시도·성공·실패·miss, gap 해소, 정책 제외와 validator 통과율의
분모·합계를 계산한다. live 시간은 미측정(`None`)으로 두고 replay 시간을 섞지 않는다.
정책에 의해 CToxPred2가 금지되거나 Decision 요청을 기다리는 경우는 step의
`excluded_capabilities`에 각각 다른 reason code로 남긴다. 원 admission이 거부된
tool call은 snapshot의 유효한 원본이 아니므로 비교를 시작하지 않는다. 이 거부는
정책적 제외나 replay 실패로 보정하지 않는다. 이 파일럿은 정책 경로의
기계적 비교이며 Decision 판단 품질 점수는 아직 제공하지 않는다. 또한
Decision 요청/응답을 새 Agent run으로 만들지 않으므로 실제 `call_number` 관계 검증은
live orchestration 경로가 담당한다.

## 전체 Decision 입력 provenance (#158)

`PolicyComparisonManifest.decision_sources`를 주면 Target Hypothesis·ADMET baseline·DTA의
실제 Agent run ID, 입력·출력 SHA-256, 출력 schema와 구현 버전을 고정한다. runner는
세 run의 terminal 상태, 동일 analysis/case, DTA→Target 계보와 ADMET run→snapshot
tool call 연결을 replay episode 생성 전에 검증한다. 누락·변조·버전 불일치에는
`decision_source_*` 고정 사유로 중단하며 provider나 Decision LLM을 재호출하지 않는다.

검증된 source를 사용하는 비교 결과의 `decision_inputs`는 기존 관측 참조에 더해
실제 source run ID 세 개와 전체 Decision 입력 JSON·SHA-256을 포함한다. Target·ADMET·
DTA 필드 축약과 prompt payload 생성은 production Decision과 공통 순수 함수를 사용한다.
`decision_recall`의 첫 Decision payload는 같은 원본을 소비한 live Decision 1번 입력과
동일하다. `baseline`과 `all_tools`는 정책상 recall을 허용하지 않아 `allow_recall=false`다.
`all_tools`와 `decision_recall`의 cardiac 관측은 CToxPred2 snapshot의 실제 tool call을
참조한다. 후속 Decision은 실제 Agent run을 만들지 않으므로 feedback은
`kind=simulated_replay_feedback`과 source tool call/호출 번호로 표시하며 live feedback의
run ID를 가장하지 않는다. 생성 step ID와 시각은 payload hash에서 제외된다.
`decision_sources`를 생략하면 기존 #155의 ADMET 관련 키만 기록하는 비교 계약을 유지한다.
## 결정적 adaptive 선택 (#156)

`AdaptiveSelectorInput`은 evidence gap, capability catalog, 서버가 고정한 tool/version
allowlist, Agent, 보유 입력 종류, 남은 도구 호출 수와 비용·지연 등급 상한을 받는다.
`select_next_tool`은 기존 `match_capabilities`의 과학적 제외 사유를 보존하고,
권한·버전·예산·자원 상한을 통과한 후보를 비용→지연→tool/version/capability ID 순서로
결정한다. LLM이나 provider를 호출하지 않으며 권한 승인 자체를 대신하지 않는다.
`PolicyComparisonRunner.run_with_adaptive`는 등록된 registry의 정규 capability 목록과
pin을 확인한 후 #155의 세 episode와 별도의 adaptive episode를 같은 snapshot으로 만든다.
선택 가능한 도구가 없으면 cardiac gap을 남기고 stop한다. replay miss는 live 호출로
전환하지 않는다. 선택 입력 JSON/hash는 episode profile과 step에 고정하며 validator가
후보, 제외 사유, gap·예산과 선택을 다시 계산한다. 이 검사는 정책 입력의 자체 일관성이지
실제 임상 안전성 평가가 아니다.

## 확장 시 주의점

- provider 원문이나 전체 SMILES를 trajectory JSON에 복사하지 않는다.
- 새 도구는 registry/admission과 typed observation 경계를 우회하지 않는다.
- `live`와 `replay`를 provider 내부 조건문으로 섞지 않고 executor에서 교체한다.
- 장문 내부 사고과정 대신 검증 가능한 `reason_code`, objective와 evidence gap을 저장한다.
- evaluation test split은 trajectory 생성 전에 case 단위로 분리한다.

## 검증

```sh
cd backend
uv run --no-env-file pytest tests/test_trajectory.py tests/test_trajectory_migration.py
uv run --no-env-file pytest tests/test_trajectory_validator.py
uv run --no-env-file pytest tests/test_admet_trajectory_replay.py
uv run --no-env-file pytest tests/test_adaptive_selector.py tests/test_adaptive_policy_replay.py
uv run --no-env-file ruff check src/evidrug_api/trajectory tests/test_trajectory.py tests/test_trajectory_migration.py
uv run --no-env-file mypy src/evidrug_api/trajectory tests/test_trajectory.py tests/test_trajectory_migration.py
```
