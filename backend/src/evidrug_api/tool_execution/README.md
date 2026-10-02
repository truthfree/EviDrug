# 공통 도구 실행 계층

ADMET·DTA의 실행 소유권과 장애 이력을 SQL에 보존한다. 모델 선택, 과학적 판단,
자동 재시도 및 전체 분석 orchestration은 담당하지 않는다.

새 도구 연결 시 [기여 안내](../../../../CONTRIBUTING.md)의 출처·라이선스·검증 원칙을 따른다.

## 구조와 진입점

`service → runner → repository → tables` 순서로 읽는다. `recover.py`는 별도 회수 진입점이다.

| 이름 / 위치 | 역할·입출력 | 오류 |
| :--- | :--- | :--- |
| `ToolExecutionRunner.execute` / runner.py | 실행 identity와 callback → typed 결과, 실행 전 commit 및 최종 원자 저장 | 실행 중 중복, lease 상실, provider/SQL 오류 |
| `ExecutionRepository.claim` / repository.py | identity → 실행 소유권, terminal replay 여부 | ID·입력 충돌, 실행 중 중복 |
| `ExecutionRepository.recover_expired` / repository.py | 만료 running → failed, 회수 개수 | DB 오류 |
| `recover_expired_executions` / recover.py | DB URL → 회수 개수, 독립 engine 정리 | DB 오류 |

## 데이터 흐름

1. 분석 입력을 확인하고 `tool_call_id`와 `request_id` 고유 제약으로 소유권을 획득한다.
2. `running`, 소유 token, 입력 hash, 시작·만료 시각을 추론 전에 commit한다.
3. 추론 중 DB transaction을 유지하지 않는다. 기존 완료 결과는 재추론 없이 사용한다.
4. 소유 token과 유효 lease를 확인하고 도메인 결과와 terminal 상태를 한 transaction으로 저장한다.
5. 예외 시 rollback 후 `failed`와 고정 오류 코드를 기록한다. DB 장애로 이것도 실패하면
   기록은 running으로 남아 이후 lease 회수 대상이 된다.

`tool_executions`는 공통 상태이고 ADMET endpoint/prediction, DTA model/observation은
기존 정규화 테이블을 그대로 사용한다. 이 ledger는 입력 원문이나 예외 원문을 저장하지 않는다.
각 동시 호출에는 독립 `AsyncSession`이 필요하다. 다른 업무의 미완료 변경을 같은 session에
넣지 않는다. repository의 `save(commit=False)`는 runner용이며 호출자가 commit/rollback한다.
직접 repository를 사용하는 기존 경로의 기본값은 `commit=True`다.

## 재전달·실패 규칙

- 같은 ID가 running이면 `ExecutionInProgress`: 기존 실행을 기다리고 새 추론은 하지 않는다.
- 같은 ID에 다른 입력·분석·run·모델 식별자가 오면 충돌이다.
- 성공한 같은 ID는 SQL 결과를 반환한다. migration 이전 결과도 검증 후 ledger에 편입한다.
- DTA provider의 unavailable은 도메인 결과도 저장하고 ledger는 failed로 기록한다. 재전달에는
  기존 unavailable을 반환한다. ADMET 예외 등 결과가 없는 실패는 `ExecutionAlreadyFailed`다.
- timeout, 취소, SQL 실패, 만료는 각각 `provider_timeout`, `cancelled`, `persistence_failed`,
  `lease_expired`다. 예측값 0이나 성공으로 바꾸지 않는다.
- 재시도는 반드시 새 request/tool call ID로 명시적으로 요청한다. 자동 재추론하지 않는다.

기본 실행 timeout은 300초, 고정 lease는 timeout+30초이며 heartbeat 갱신은 없다.
서비스 생성자의 `timeout_seconds`로 조절한다. 내부 provider timeout이 더 짧으면 먼저 적용된다.
비동기 timeout은 CPU blocking 작업/외부 원격 작업의 강제 중지를 보장하지 않는다.
lease가 만료된 이전 실행은 결과를 늦게 반환해도 저장할 수 없다. 물리적 계산 exactly-once나
서로 다른 ID 간 동시 추론 제한을 보장하는 기능은 아니다.

## 회수 실행 및 검증

신규 claim 직전에도 만료 작업을 회수한다. 요청이 없어도 회수하려면 Celery worker와
Beat를 함께 실행해야 한다. Beat는 60초마다 `evidrug.tools.recover_expired`를 enqueue한다.
worker가 바쁘거나 중단되면 실제 회수는 지연된다. 수동으로는 backend에서 실행한다.

```sh
uv run python -m evidrug_api.tool_execution.recover
uv run --no-env-file pytest tests/test_tool_execution.py tests/test_admet_repository.py tests/test_dta.py
```

먼저 `alembic upgrade head`로 migration을 적용한다. downgrade는 ledger 이력을 제거하지만
기존 모델 예측 테이블은 유지한다. 테스트는 합성 provider와 SQLite이며 Docker나 API key가
필요 없다. 실제 PostgreSQL/Redis Beat 배포 검증은 별도로 필요하다.

프로세스 강제 종료는 lease_expired로만 알 수 있다. OOM 여부는 플랫폼 로그로 확인해야 한다.
이 작업은 `analyses`의 전체 상태를 변경하거나 Render를 재시작하지 않는다.
관련: [기능 명세](../../../../docs/features/tool-execution-lifecycle.md), Issue #77.
