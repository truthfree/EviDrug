# DTA shortlist Agent (#96)

저장된 Target primary/alternatives의 서열과 출처를 검증한 뒤 후보별 예측을 순차 수행한다.
ADMET·Decision 구현과 worker 배포는 #109, 저장 결과 재사용 continuation은 #110이다.
이 브랜치만으로 운영 worker에 DTA가 자동 등록되지는 않는다.

## 실행 계약

- `DtaAgent.execute`: 검증된 Target upstream → 후보별 typed 결과와 한 번의 LLM 해석.
- `upstream.load_upstream`: 동일 분석의 입력·출력 hash, 소유권과 schema 검사. 교차 분석 거부.
- `FixedToolRun`: 기존 admission/runner/SQL을 사용하며 후보별 독립 tool/request ID를 생성한다.
- `DaconSpecialistReasoner`: 전체 서열 없이 관측만 전달, 출력 1200 token 상한, 자동 retry 없음.
- 후보 예산 초과는 skipped, 일부 성공은 partial_failure, 전부 실패는 failed로 보존한다.
- 예측 단위·모델·출처를 유지하며 실패를 0으로 바꾸거나 서로 다른 score를 평균하지 않는다.

공통 fixed-tool/upstream/해석 보조 모듈은 DTA의 직접 의존성으로 포함한다. #109에서 재사용한다.
호출/토큰 비용 검증을 위해 gateway의 completed 상태 전달도 함께 포함한다.

## 검증

`tests/test_dta_agent.py`, `test_poc_reasoning.py`, 기존 DTA/provider/ledger 테스트로 검증한다.
외부 호출 없는 테스트로 전체/부분/전체 실패, 후보 수 제한, 원본 변조, SQL 저장과 재전달을 확인한다.
이미 합쳐진 개발 코드의 사용자 실행에서는 palbociclib–CDK4 predicted_pkd=4.811026573181152와
SQL 저장 및 DTA 해석 완료를 확인했다. 이는 단일 후보의 실측이며 다중 후보 실모델 검증이나
이 분리 브랜치만의 worker 배포 성공으로 확대 해석하지 않는다.

## 해석 실패 진단 (#114)

DTA v2는 예측 성공과 LLM 해석 실패를 구분한다. 해석 실패 시 성공 관측과 사용량을
보존하고 interpretation=null, partial_failure로 기록한다. 후보 실패/생략도 함께 있으면
오류 문구에 두 문제를 모두 표시한다. 전체 예측 실패 시 해석은 호출하지 않는다.

기존 error.code=reasoning_invalid_output은 유지하며 warnings[].code로 아래 사유를 기록한다.

- reasoning_response_incomplete: 응답이 completed가 아님
- reasoning_json_invalid: JSON 구문 오류
- reasoning_schema_invalid: 필드·자료형·길이 등 출력 계약 위반
- reasoning_citation_unknown: 제공되지 않은 근거 ID 인용
- reasoning_observation_citation_missing: 실제 관측 대신 model_context만 인용

검사 순서에서 먼저 발견된 사유를 남긴다. 응답/예외 원문은 저장하지 않고 자동 retry나
임의 보정을 하지 않는다. 프롬프트·모델·검증 허용 조건은 변경하지 않았다.
과거 reasoning_invalid_output 기록은 세부 사유가 없으므로 소급 확정할 수 없다.
진단 개선이지 LLM 출력 실패 자체의 해결을 보장하는 변경은 아니다.

## 인용 계약 명시와 안전한 진단 (#124)

전문 해석 프롬프트 `poc-specialist-v2`는 `allowed_evidence_ids`와
`observation_evidence_ids`를 근거 객체와 별도로 전달한다. DTA에서는 후보별 최상위
Ensembl ID가 두 목록에 들어간다. 후보 안의 `ensembl_id`, `approved_symbol`, score 종류,
도구 호출 ID 같은 중첩 값은 인용 ID가 아니다. ADMET에서는 최상위 endpoint ID가 관측 ID이고
`model_context`는 허용된 배경 근거지만 단독 인용으로는 충분하지 않다.

검증기는 여전히 제공되지 않은 ID와 관측 없는 인용을 거부한다. 거부 시 출력 원문이나
잘못된 ID를 저장하지 않고 warning에 허용 ID 수와 미허용 ID 수, 또는 관측 ID 수만 남긴다.
예측값·원 도구 참조, 해석 실패 상태, 사용 토큰과 프롬프트 버전은 별도로 보존한다.
이 변경은 과거 실패의 정확한 원인을 복원하거나 새 실모델 실행의 성공을 보장하지 않는다.
무료 회귀 테스트로 계약과 저장 결과를 확인한 뒤, 실제 일반 실행은 별도 통합 검증에서 확인한다.

관련: [#96](https://github.com/truthfree/EviDrug-Dacon2026/issues/96),
[#109](https://github.com/truthfree/EviDrug-Dacon2026/issues/109).

## BindingDB dual-model 확장 (#168)

DTA Agent는 `CNN_CNN_BindingDB`와 `MPNN_CNN_BindingDB`를 후보별로 독립 tool call로
실행한다. 두 결과는 `model_runs`에 모델 ID·artifact hash·tool call ID·원 pKd를
각각 보존하고 평균하지 않는다. 하나만 성공하면 성공 관측은 유지하되 Agent run은
`partial_failure`/`dta_models_incomplete`로 모델 공백을 드러낸다. 기존 후보 예산은 모델
호출당 하나씩 차감한다. MPNN-CNN 실모델 자원·latency smoke와 운영 배포는 별도
검증 조건으로 남긴다.
