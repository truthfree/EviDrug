# Agent별 도구 등록과 실행 승인

## 목표와 범위

Agent가 도구 호출을 생성하더라도 서버가 허용한 범위 내에서만 실행한다.
Hypothesis Agent를 포함한 전체 흐름은 유지하고, 이번 변경은 전문 Agent의 도구 실행 진입점이다.
등록·schema 노출·권한·고정 버전·입력 검증, run별 호출/시간 예산과 SQL 승인 기록을 구현한다.
LLM Agent 루프·Hypothesis 도구·전체 orchestration·조회/요약 도구·Render 배포는 제외한다.

## 정상 흐름과 입력·출력

서버가 인증된 분석 context와 run 정책을 SQL에 고정한다. 같은 정책에서 노출할 도구 schema와
실행 허용 목록을 파생한다. Agent의 typed ToolRequest를 검증·승인하면 기존 ADMET/DTA
service로 전달하고 공통 ToolObservation을 반환한다. 현재 HTTP API와 UI는 변경하지 않는다.

Hypothesis와 Decision은 기본 예측 권한을 갖지 않는다. 등록과 권한은 별개이며 신규 도구마다
허용 Agent를 선언하고 run 정책에서도 명시적으로 활성화해야 한다.

## 오류와 비용

거부 코드는 tool_not_allowed, tool_version_mismatch, tool_unavailable, invalid_arguments,
run_mismatch, tool_budget_exhausted, deadline_exceeded, request_conflict다.
거부된 요청은 provider를 실행하지 않는다. 승인 후 provider 실패는 별도 failed 관측이며
예측값 0으로 대체하지 않는다. 상세 예외·민감 입력은 승인 기록/관측에 노출하지 않는다.

승인과 예산 차감은 원자적이며 거부·재전달은 추가 차감하지 않는다. 승인 후 실패도 예산을
소비하고 재시도는 새 ID를 사용한다. 정책을 같은 run에서 바꿔 예산을 초기화할 수 없다.
새 run 생성과 analysis 전체 token·비용·recall 제한은 후속 orchestration의 책임이다.
허용 목록만 변경하는 ablation을 지원하며 fixed/adaptive/replay 전체 평가 기능은 후속이다.

## 완료 조건

- ADMET·DTA가 공통 registry/admission을 거쳐 기존 SQL service로 연결된다.
- Agent 권한·입력·version·deadline·예산 거부가 실제 provider 호출을 차단한다.
- 동시 요청이 호출 상한을 넘지 않고 재시작·재전달이 예산을 초기화하지 않는다.
- ADMET typed 결과 직렬화와 DTA unavailable의 실패 변환을 검증한다.
- 정책 변경 거부, SQL 저장 실패 rollback, 취소와 timeout을 검증한다.
- migration·기존 backend 회귀 테스트를 통과한다. 운영 PostgreSQL/Render 실검증과 구분한다.

백엔드 Issue #79.
프론트엔드 변경 없음. 상세 [구현 안내](../../backend/src/evidrug_api/tool_admission/README.md).
