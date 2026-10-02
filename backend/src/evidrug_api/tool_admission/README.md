# ToolRegistry와 실행 승인

서버가 허용한 Agent·도구·버전·입력·예산만 실행한다. Hypothesis → 전문 Agent → Decision의
전체 orchestration 또는 LLM Agent 루프는 이 모듈의 책임이 아니다.

## 구조와 진입점

`contracts → registry → repository → executor → bindings → capabilities` 순서로 읽는다.

| 진입점 / 위치 | 역할·입출력 | 오류 |
| :--- | :--- | :--- |
| `ToolRegistry` / registry.py | 고정 binding 목록, 허용 schema·입력 검증 | 중복/잘못된 등록, 정책 오류 |
| `AdmittedToolExecutor.start_run` / executor.py | 서버 ToolRunPolicy → 불변 SQL snapshot | 재설정·미등록 버전·Agent 권한 오류 |
| `AdmittedToolExecutor.schemas` / executor.py | 서버 context → 허용 도구의 SDK 중립 JSON schema | 없는 run·context 불일치 |
| `AdmittedToolExecutor.execute` / executor.py | context·call ID·typed ToolRequest → ToolObservation | DB 장애·잘못된 서버 context·취소는 전파 |
| `admet_binding`, `dta_binding`, `ctoxpred2_binding` / bindings.py | provider adapter → 기존 SQL service 연결 | 도메인 실패는 failed observation |
| `ToolCapability`, `EvidenceGap`, matcher / capabilities.py | 근거 공백 → 실행 가능한 후보와 고정 제외 사유 | 입력·endpoint·단위·Agent 불일치 |

등록은 권한 부여가 아니다. binding의 허용 Agent와 저장된 run allowlist를 모두 만족해야 한다.
현재 binding은 `admet → admet_ai`, `admet recall → ctoxpred2`, `dta → dta`를 허용한다.
`target_hypothesis`와 `decision`은 빈 목록으로 시작할 수 있으며 예측 도구 권한이 없다.
추후 Hypothesis 전용 도구는 해당 Agent를 허용하는 별도 binding으로 추가한다.

## 서버에서 연결하는 순서

1. 고정 runtime/provider adapter로 `ToolRegistry`와 필요한 세 binding을 만든다.
2. orchestration이 인증된 분석 소유권을 확인하고 `RunContext(analysis_id, run_id, agent)`를 만든다.
3. context, policy version, 허용 tool/version, max_tool_calls, 절대 deadline,
   call_timeout_seconds로 `ToolRunPolicy`를 생성하여 `start_run()`한다.
4. 저장된 정책에서 `schemas(context)`를 얻어 Agent SDK 경계에 전달한다.
5. LLM이 요청한 `ToolRequest`를 검증한 후 같은 서버 context와 안정적인 tool_call_id로
   `execute()`한다. 요청의 run_id는 서버 context와 일치해야 한다.

context/policy/handler/argv는 LLM/HTTP 입력에서 만들지 않는다. 인증을 대신하는 HTTP API가
아니며 현재 공개 endpoint도 없다. session은 호출별로 독립 생성하고 외부 업무 변경을 섞지 않는다.
상위 실행자는 동일 request에 동일 tool_call_id를 유지해야 한다. 변경하면 충돌로 거부한다.
top-level envelope 파싱 실패는 호출 이전 경계에서 처리하며 본 계층은 유효한 ToolRequest를 받는다.

## 승인·예산과 재전달

`tool_admission_runs`는 run당 정책 JSON snapshot·hash·예산·deadline을 저장한다.
반복 endpoint/예측 JSON 저장이 아니라 설정 1회 기록이다. 같은 run을 다른 정책으로 초기화할 수 없다.
`tool_admissions`는 request당 ID·tool/version·입력 포함 요청 hash·승인/거부 코드·시각을 저장한다.
정책은 run FK로 찾는다. 민감한 입력·objective 원문과 예외 원문은 저장하지 않는다.

조건부 SQL UPDATE로 예산을 예약하고 승인 행과 함께 commit한다. 동시 worker에서도 예산을
초과하지 않으며 unique 충돌 시 transaction 전체를 rollback하고 기존 결정을 읽는다.
승인 후 실패·취소·프로세스 종료가 생겨도 슬롯을 환불하지 않는다. 실행 여부가 불확실한
요청을 환불해 예산을 우회하는 것을 막기 위한 보수적 정책이다.

- 거부된 새 요청과 동일 요청 재전달은 추가 차감하지 않는다.
- 같은 ID의 다른 내용은 request_conflict다. 원래 승인 행을 덮어쓰지 않으며 충돌 시도별
  append-only trace는 후속 기능이다.
- 승인 후 crash, 실행 전 crash라도 동일 요청을 재전달해 기존 runner로 진입할 수 있다.
  running 중복·완료 결과·실패 재전달은 기존 실행 ledger가 처리한다.
- 재계산 retry는 새 request/tool call ID와 남은 예산이 필요하다. 자동 retry/fallback은 없다.
- 이 상한은 **하나의 Agent run**에 대한 것이다. 전체 analysis token·비용·recall 상한과
  새로운 run 생성 권한은 후속 orchestration에서 제한해야 한다.

`execution_metadata.usage.tool_calls`는 이번 전달에서 새로 예약한 논리 호출 슬롯 수(0 또는 1)다.
실제 모델 추론 횟수·cache hit·토큰·과금 계측이 아니다. 승인 직후 crash하면 응답 metadata가
없을 수 있으므로 예산 집계의 원본은 SQL이다. 현재 bindings는 로컬 provider용이며 외부 API
binding은 external_requests·원격 idempotency·비용 추적을 추가해야 한다.

## 시간·실패·출력

승인 시 deadline을 확인하고 실행 직전 다시 검사한다. service에 전달하는 추론 timeout은
남은 시간, run별 timeout, binding별 상한 중 최솟값이다. SQL 저장·정리 시간은 추가될 수 있다.
deadline 이후에는 이전 승인·완료 호출도 execute로 재전달하지 않는다. 별도 결과 조회는 후속 단계다.
binding은 반드시 Invocation.timeout_seconds를 기존 runner에 전달해야 하며 registry는 임의
handler를 안전한 sandbox로 바꾸지 않는다. binding은 신뢰된 서버 코드다.

거부는 `rejected`, 승인 후 장애는 `failed`, 정상 결과는 `succeeded`로 변환한다.
DTA unavailable은 DB에 원 계약대로 남지만 observation에는 result 없이 error만 반환한다.
`execution_in_progress`는 현재 전달의 failed observation이지 기존 running 행을 실패로 바꾸는
뜻이 아니다. 취소는 상위 호출자에게 전파하고 기존 runner가 실행 실패를 기록한다.
DB 장애가 나면 승인 없이 실행하지 않는다.

ADMET는 manifest를 제외한 typed prediction 결과를 반환한다. 필요한 endpoint만 선택하는
ContextBuilder는 [ADMET 읽기 전용 기반](../admet/README.md)에 구현되어 있지만 이 executor와
조회 tool로 연결하지는 않았다. ArtifactReference 등록·조회 API도 아직 포함하지 않는다.
여러 result 타입을 공통 envelope에 담아도 실제 필드가 직렬화되도록 SerializeAsAny를 사용하며,
결과는 먼저 등록 result 모델로 재검증한다. SDK에 이 관측 전체를 무조건 prompt로 넣지 않는다.

DTA tool version은 provider·model ID·version·artifact hash 전체의 SHA-256으로 고정한다.
실제 모델 출처는 도메인 결과에 남는다. ADMET는 기존 1.4.0 binding만 지원한다.
fixed/adaptive 선택과 recall 정책은 아직 구현하지 않았다. allowed_tools를 비우거나 줄이는
도구 ablation만 현재 정책으로 지원하며 provider 코드를 변경할 필요가 없다.

## Capability와 evidence gap

`capabilities_for_bindings()`는 현재 등록된 `admet_ai`, `ctoxpred2`, `dta`의 tool/version을
그대로 사용해 typed capability catalog를 만든다. metadata는 질문 범위, 필수 입력,
endpoint·필드·단위, provenance, 실행 위치와 대략적 비용/지연 등급을 설명할 뿐 실행 권한이
아니다. `validate_capabilities()`는 catalog의 tool/version/Agent가 `ToolRegistry`와 정확히
일치하는지 검사한다.

`match_capabilities()`는 LLM이나 embedding 없이 모든 capability를 안정적인 순서로 검사한다.
match와 함께 입력 부족, 명시적 범위 밖, endpoint·field·단위 불일치, Agent 불일치를 고정
reason code로 반환한다. match가 없으면 기본 도구를 선택하거나 live provider로 fallback하지
않는다. 유효한 match만 `TrajectoryActionCandidate`로 투영할 수 있으며 실제 실행은 계속
registry allowlist, admission과 예산 검사를 통과해야 한다.

## 검증과 확장

```sh
uv run --no-env-file pytest tests/test_tool_admission.py
uv run alembic upgrade head
```

새 migration은 `0005_tool_admission`이다. downgrade는 승인·예산 기록을 삭제하므로 활성 run이
있는 운영 DB에서 수행하면 안 된다. 실제 PostgreSQL 경쟁·잠금 및 worker 운영 검증은 별도로 필요하다.
테스트는 fake provider와 파일 SQLite를 쓰며 실제 Docker/API key가 필요 없다.
새 도구는 [실행 계약](../execution_contracts/README.md)과
[기여 안내](../../../../CONTRIBUTING.md)를 따른다.
관련: Issue #79,
[기능 명세](../../../../docs/features/tool-admission.md).
