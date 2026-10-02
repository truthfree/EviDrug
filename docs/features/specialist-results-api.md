# 전문 Agent 결과 조회 API (#111)

## 목적과 범위

저장된 ADMET·DTA·Decision 결과를 #112 화면에서 보여주기 위한 읽기 전용 계약이다.
결과 조회는 provider/LLM/queue를 호출하지 않고 SQL만 읽는다. 기존 polling 응답을
크게 만들지 않도록 GET /api/v1/analyses/{analysis_id}/results를 별도로 제공한다.
Target 상세는 기존 상태 조회 응답을 유지한다. UI·공개 재실행·자동 캐시는 제외한다.

## 접근과 정상 흐름

분석 생성 당시 세션 쿠키의 fingerprint 또는 유효한 동일 브라우저 ID를 확인한다.
기존 상태 조회에도 동일하게 적용한다. 미인증은 401, 없는 분석·다른 방문자·CLI replay
분석은 모두 404다. 브라우저 ID 도입 이후 생성한 분석은 같은 브라우저에서 재로그인해도
조회할 수 있다. 구형 세션의 기록은 새 방문자에게 임의 귀속하지 않으며, 개인 계정 기반
공유·다른 기기 복구는 후속 범위다.
CLI continuation은 웹 세션 소유가 아니므로 공개 API로 노출하지 않고 CLI show로 확인한다.

## 응답 계약

- analysis_id, 전체 status와 기존 화면용 admet/dta/decision 세 결과 슬롯, 순서형 `calls`.
- `calls`는 Target/ADMET/DTA/Decision의 저장 run을 Agent별 `call_number` 순으로 보여준다.
  각 항목은 목적, 요청한 Decision run ID, 응답한 Agent run ID, 결과 종류·상태를 기록한다.
  Decision 1번 요청에 대한 ADMET 2번 응답과 Decision 2번 재판단을 같은 run ID 관계로
  추적한다. 첫 Decision은 분석당 요청 한 건만 허용하므로 요청 run ID가 상관 ID다.
  Target 상세 결과는 기존 상태 조회에서 유지하고 `calls`에는 실행 참조만 표시한다.
- ADMET 2번 결과는 `cardiac_recall` 고정 슬롯 대신 `calls`의 결과로 노출한다. 향후 다른
  근거 요청은 새로운 호출과 결과 종류를 추가하되 같은 순서형 계약을 사용한다.
- 요청 `objective`는 저장된 Decision 출력에서 온 비신뢰 텍스트다. 화면에 표시할 때는
  다른 LLM 해석 문장과 마찬가지로 HTML로 실행하지 않는다.
- 각 슬롯은 최신 attempt의 run_id, 실행 status, projection_status, result, error_code,
  warning_codes와 해당 run의 token_usage를 제공한다.
- 아직 run이 없으면 run_id/status/result/token_usage=null, projection_status=unavailable.
- running·출력 없는 실패·과거 출력 부재는 결과 없음으로 처리한다.
- JSON/schema/식별자/status/hash 불일치는 projection_status=invalid로 표시하고
  result와 출력 기반 진단/사용량을 노출하지 않는다. 다른 전문 결과 조회는 유지한다.
- 최신 attempt가 실패해도 이전 성공 결과로 대체하지 않는다.
- ADMET: 선택 endpoint의 수치·단위·참조 백분위·출처, 선택 수/전체 수, 누락 항목,
  해석·한계. 선택 범위가 작음과 실행 실패를 구분한다.
- DTA: 후보별 상태·점수 유형·단위·모델명/버전·도구 실행 참조, 해석·한계.
- Decision: 연구용 verdict, 설명, 인용 근거 ID, conflicts/gaps, 제안 상태의 구조화된
  `next_actions` 1~3개, 원본 전문 run 참조. 각 후속 행동은 수행 내용, 필요 이유와 판정 변경
  관측을 분리한다. 행동 종류는 폐쇄형 enum으로 제한하지 않아 사례에 맞게 내부 불확실성 해소,
  외부 실험·조사 또는 대안 탐색을 함께 제안할 수 있다. v3 이하 저장 결과는
  `next_actions=[]`로 호환 조회한다.
- source_run_ids/tool_call_id는 출처 참조이지 해당 ID에 대한 접근 권한 부여가 아니다.
- token_usage는 해당 run의 기록된 비용이다. 모르면 null이며 0으로 추정하지 않는다.
  조회 비용은 0이고, 응답을 반복해서 받았다고 사용량을 합산하면 안 된다.
  CLI reused_runs의 과거 비용과 new_runs의 신규 비용은 기존 CLI 계약을 따른다.

서열·서열 hash·artifact/manifest hash·세션 fingerprint·입력/원문 JSON·예외 메시지를
공개 DTO에 넣지 않는다. 저장된 해석 문장은 비신뢰 텍스트이며 UI는 HTML로 실행하지 않는다.
기존 result가 null이거나 지원하지 않는 과거 계약은 실패 없이 unavailable/invalid로 구분한다.
DB migration과 새 외부 의존성은 없다. 필드의 최종 명세는 FastAPI OpenAPI를 따른다.

## 검증과 코드 읽는 순서

router → 소유권 검사 → repository 최신 run 조회 → results projection → result_models.
접근 제어, 전체/부분/실패, 과거/null/손상 출력, 민감 필드 제외, 최신 attempt 우선,
반복 조회 시 추가 실행 없음과 OpenAPI를 회귀 테스트한다.
관련: #109, #110, #111, #112.

## 검증 기록 (2026-09-22)

- backend 전체 300 passed / 3 skipped, Ruff check/format 및 mypy 통과.
- HTTP 테스트에서 소유 세션/다른 세션/미인증/CLI replay 접근, 부분 실패, 과거/null/손상
  출력과 최신 실패 attempt 우선, 공개 OpenAPI, no-store 및 민감 필드 비노출을 확인했다.
- 반복 GET의 SQL 구문에 INSERT/UPDATE/DELETE가 없고 provider·해석·dispatcher 호출 수가
  증가하지 않음을 검증했다.
- 개발 PostgreSQL에 저장된 실제 ADMET·DTA·Decision 5건을 읽기 전용으로 투영했다.
  완료/부분 실패는 결과를, 실패는 결과 없음과 기록된 비용(없으면 null)을 반환했다.
  이는 projection 검증이며 CLI 분석을 웹에 공개했다는 뜻이 아니다.
- Docker API 재빌드 후 health 200, OpenAPI 경로 등록, 미인증 결과 조회 401을 확인했다.
  사용자가 원래 로그인된 브라우저에서 원본 분석의 결과 JSON을 조회했다.
  ADMET 완료 결과(선택 12/49, 누락 없음), 과거 DTA/Decision 실패와 당시 사용량이
  올바르게 구분됐다. 이후 #112/PR #119에서 [실제 결과 카드 UI](specialist-results-ui.md)를 연결했다.
- 애플리케이션 LLM 추가 호출 0회, 개발 PostgreSQL 쓰기 0회.
