# Dacon OpenAI Responses 연동

Dacon이 제공하는 OpenAI 호환 게이트웨이와 통신하는 모듈이다. 상위의 hypothesis
agent가 SDK나 인증 헤더를 직접 알지 않도록 외부 API 경계를 한곳에 모은다.

## 책임

- `build_dacon_openai_client`: 설정을 읽어 OpenAI SDK와 `api-key` 헤더를 구성한다.
- `build_dacon_openai_sdk`: 호환성 검증과 runtime adapter에 동일한 저수준 SDK를 제공한다.
- `DaconOpenAIClient.generate_text`: Responses API를 비동기로 호출한다.
- `GeneratedText`: 생성된 텍스트와 모델, 응답 ID를 전달한다.
- `ResponseUsage`: 요청이 사용한 입력·출력·전체 토큰 수를 전달한다.
- `DaconQuota`: Dacon 응답 헤더의 전체/단시간 잔여량을 전달한다.

질환 해석, 프롬프트 작성, 재시도 여부 판단은 이 모듈의 책임이 아니다. 해당 로직은
각 에이전트 또는 orchestration 계층에 둔다.

## 환경변수

실제 키는 커밋하지 않는 `.env`의 `EVIDRUG_OPENAI_API_KEY`에만 둔다. 예시는
`backend/.env.example`에 있으며 실제 값은 비워 둔다. 기본 모델은
`gpt-5.6-sol`이고 `EVIDRUG_OPENAI_MODEL`로 지원 모델 중 하나를 선택할 수 있다.

백엔드를 `backend` 디렉터리에서 직접 실행할 때는 `backend/.env`를 사용한다. Docker
Compose로 실행할 때는 저장소 최상위 `.env`를 사용하며, 같은 설정이 API와 worker
컨테이너에 전달된다.

클라이언트는 SDK 기본 `Authorization` 헤더와 함께 Dacon에서 요구하는 `api-key`
헤더를 설정한다. 키 값이나 요청 전문을 로그로 남기지 않는다.

현재 단계에서는 streaming과 공개 API endpoint를 제공하지 않는다. 다음 기능에서
hypothesis agent가 이 클라이언트를 사용하고, 그 후 별도 backend API로 노출한다.

Tool calling은 아직 production Agent 계약으로 제공하지 않는다. Dacon gateway의 strict
function call과 연속 응답 지원 여부는
[`experiments/dacon-agent-runtime-compatibility`][runtime-probe]의
고정 smoke에서 먼저 검증한다. 저수준 SDK builder를 일반 도메인 코드에서 직접 호출하지
않으며, 검증 후 선정할 Agent runtime adapter 안으로 제한한다.

[runtime-probe]: ../../../../experiments/dacon-agent-runtime-compatibility/README.md

## 전문 해석 strict JSON 출력 (#301)

`generate_text(..., output_schema=Interpretation)`는 Pydantic 계약에서 schema를 파생하고
Responses 요청의 `text.format`에 `json_schema`, `strict=true`를 전달한다.
추가 키는 `additionalProperties=false`로 차단한다. schema를 전달하지 않은 기존 호출의
요청 형식은 변경하지 않는다. ADME/TOXICITY만 이 모드를 사용하고 DTA·Decision은 기존
별도 검증 경로를 유지한다. schema 바이트도 specialist 입력 예산에 포함한다.

구조화 출력 방식은 [공식 OpenAI 문서](https://developers.openai.com/api/docs/guides/structured-outputs)
를 따른다. Dacon 게이트웨이의 실제 지원 여부는 별도 유료 smoke로 확인해야 한다.
지원되지 않아 요청이 거부되면 `model_request_rejected`로 실패하며 일반 텍스트 fallback이나
두 번째 생성 호출을 자동 실행하지 않는다. 기존 SDK의 기술적 retry 설정은 그대로다.
실제 검증 smoke는 max_retries=0으로 실행해야 한다.

`GeneratedText`는 거부 여부와 미완료 사유를 전달한다. specialist는 생성 거부를
`reasoning_response_refused`, 응답 미완료를 `reasoning_response_incomplete`로 구분한다.
문법 오류는 `reasoning_json_invalid`로 처리하며 응답 UTF-8 길이·문자 수·파싱 위치·행·열과
단일 코드블록 여부만 경고에 보존한다. 위치는 파싱 대상 JSON 문서 기준이다.
원문, 예외의 문서/메시지, API 키, 전체 SMILES는 저장하지 않는다.
strict schema를 요청했더라도 기존 길이·인용·수치 표시 검증은 계속 수행한다.

완료/부분 실패 비용과 원 ADMET 관측 보존 정책은 변경하지 않는다.
실행 component `specialist_output_contract=admet-interpretation-json-schema-v1`로 새 계약을
식별한다. schema 지원을 확인하기 전에는 실제 오류 재발 방지가 검증됐다고 선언하지 않는다.

2026-10-01 승인된 합성 데이터 smoke에서 Dacon `gpt-5.6-sol`로 ADME/TOXICITY를 각 1회
호출하여 해당 schema 지원과 schema·인용·숫자 표시 검증을 확인했다.
max_retries=0, 출력 각 최대 2,048 tokens, 총 사용량 2,352 tokens였다.
이는 API 호환성 확인이며 새 코드 배포·전체 분석 실행 성공을 의미하지 않는다.

같은 날 사용자가 재빌드한 통합 작업본에서 PARP1 분석
`d65772c9-5d91-477b-8c45-6363d47c21ae`와 PIK3CA 분석
`8ae14cfc-579d-492d-9ae3-c9beb99d8277`이 완료된 것을 저장 이력으로 확인했다.
두 실행의 ADME·TOXICITY는 새 출력 계약 component를 기록했고 ADMET 경고는 없었다.
이는 통합 작업본의 런타임 증거이며, 이 PR만 적용한 빌드의 배포 검증이나 #300의
지정 개발 사례 각각 3회 반복 검증을 대신하지 않는다.
합성 smoke의 비용·호환성 기록은
[`issue301-structured-output-smoke-20261001.json`](../../../../docs/evaluation/issue301-structured-output-smoke-20261001.json)에 보존한다.
