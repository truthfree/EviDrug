# Breast cancer PoC 전체 실행 연결 (#109)

## 현재 상태 (2026-09-22)

PR #116 구현, #117 continuation, #118 결과 API, #119 카드 UI가 dev에 병합됐다.
일반 실행의 최신 사례는 `086220de-1891-41c8-b6d8-e03865f8175a`이며 Target·ADMET·Decision 완료,
DTA 예측 성공/해석 실패로 `partial_failure`다. 전체 성공으로 표시하지 않는다.
최신 검증 상세와 비용은 [프로젝트 현황](../project-status.md)을 기준으로 본다.

## 배경과 목표

2026-09-22 점검 시 worker는 Target만 등록했다. 최신 개발 DB에서도 Target만 completed이고
ADMET/DTA/Decision은 `agent_not_configured`였다. 기존 모델 adapter, provider, admission,
ledger와 SQL 저장은 구현되어 있었으나 Agent와 worker 배포 연결이 없었다.

이번 범위는 ADMET·Decision Agent와 네 단계 worker 연결, 실모델 배포다.
shortlist DTA Agent는 별도 #96 브랜치의 선행 변경이며 이 이슈에 흡수하지 않는다.
임상적 효능 확정, 자동 retry/fallback, Decision recall, 프론트엔드 전문 결과 상세 카드와
자동 캐시는 제외한다.

## 실행 구조

1. Target과 ADMET을 시작한다. Target은 기존 Open Targets/UniProt/LLM 경로를 유지한다.
2. ADMET는 고정 binding으로 모든 원 예측을 SQL에 저장한다. 현재 16개 선택 endpoint만
   ContextBuilder로 읽어 ADME·독성을 각각 해석한다. 선택 endpoint 누락은 부분 실패로 남긴다.
3. DTA는 저장 Target의 artifact/hash/소유권/입력/typed shortlist를 검증한다. 후보를 순차
   실행하며 후보 수와 max_tool_calls 중 작은 수만 실행한다. 나머지는 명시적으로 skip한다.
4. 후보별 score type, unit, 모델/version, tool call ID와 실패를 보존한다. 서열 원문은
   도구/SQL에만 사용하고 LLM에 넣지 않는다. 실패를 score 0으로 바꾸지 않는다.
5. Decision은 저장 전문 결과의 작은 projection만 한 번 종합한다. 근거·방향 미확정,
   전문 단계 누락/부분 실패가 있으면 무조건적인 go를 거부한다.
6. Decision의 final과 go/conditional_go/no_go는 연구 우선순위 판단이다. workflow completed는
   임상 성공이나 Go를 뜻하지 않는다. 일부 전문 단계 실패는 partial_failure로 보존한다.

예측은 기존 ToolRegistry → admission → service → ToolExecutionRunner → repository를 통과한다.
Agent는 직접 subprocess/HTTP를 실행하지 않는다. 후보별 tool ID는 run과 Ensembl ID에서
결정적으로 생성한다. terminal 분석 재전달은 다시 호출하지 않는다.

## 비용·자원

- 도구 선택 LLM은 없다. 정상 live 실행은 Target/ADMET/DTA/Decision 각 최대 1회, 총 4회
  해석 요청이다. retry=0인 기존 Target runtime client를 공유한다.
- 신규 전문 해석 출력은 각 1,200 token, Decision은 1,800 token 상한이다.
- instructions를 포함한 UTF-8 byte 기반 입력 가드는 전문 해석 24,000, Decision 32,000이다.
  정확한 token 계산이 아닌 보수적 가드다. 초과 시 자동 절단/재호출 없이 실패한다.
- 원 manifest, 서열, 전체 Target evidence를 Decision에 반복 전달하지 않는다.
- 응답의 input/output/total token을 기록한다. 파싱/인용 실패도 사용량을 보존한다.
  네트워크 실패로 응답을 받지 못한 경우 사용량은 0이 아닌 미상(null)이다.
- OpenAI Docs에 따라 출력 한도는 추론 토큰도 포함하고 incomplete 응답은 성공으로
  인정하지 않는다. [공식 문서](https://developers.openai.com/api/docs/guides/reasoning)
- ADMET/DTA stage는 공유 lock으로 모델 상주를 직렬화하고 stage 종료/취소 시 모델을 닫는다.
  DTA 후보 사이의 warm 재사용은 유지한다. 기본 worker는 concurrency=1, 1 CPU/2 GiB다.
  이 한도에서 결합 이미지의 실제 peak RSS/timeout은 통합 검증이 필요하다.
- stage timeout=300초에는 lock 대기도 포함한다. 분석 lease=1,200초다.

## 실행 방법

기본 Compose는 가벼운 기존 Target-only 환경을 유지한다. 실행 중인 분석이 없는 상태에서
다음 명령으로 실모델 worker를 준비하고 교체한다. 첫 빌드는 시간과 디스크 공간이 필요하다.

```sh
bash scripts/build-poc.sh
docker compose -f compose.yaml -f compose.poc.yaml up -d --build
```

Linux amd64 이미지이므로 Apple Silicon에서는 에뮬레이션 비용이 있다. 기존 DB 볼륨은 유지한다.
`down -v`는 필요 없다. override가 worker에만 `EVIDRUG_POC_MODELS_ENABLED=true`를 주입한다.
모델 argv는 서버 고정값이며 요청/LLM이 변경할 수 없다. subprocess는 API key를 상속하지 않는다.

UI에서 breast cancer / CDK4 / palbociclib 입력을 확인해 실제 분석을 한 번 실행한다.
분자 구조는 기존 palbociclib fixture와 입력 검증을 사용한다. 지정 타깃 1개로 경계를 먼저
확인한 후 discover shortlist를 검증한다. 목업은 실모델 검증이 아니다.

확인 대상은 agent_runs의 네 단계 결과와 usage, admet_predictions, dta_observations,
tool_admissions/tool_executions, execution_trace_events다. 현재 화면은 Target 상세와
[ADMET/DTA/Decision 카드](specialist-results-ui.md)를 제공한다. 내부 진단은 SQL 기록과 대조한다.

저장 Target/ADMET(LLM 해석 포함)를 재사용하는 [continuation CLI](agent-replay.md)는 #110에서 구현·병합했다.
화면의 전체 DAG는 여전히 자동 cache가 없으며 다른 분석의 upstream을 암묵적으로 읽지 않는다.

## 코드 읽는 순서·수정 위치

### DTA 결과 디렉터리 권한 수정 후 검증

사용자 단독 진단에서 DeepPurpose 0.1.5의 `DTI.py:292` (`os.mkdir(result_folder)`)가
`PermissionError: errno 13`으로 실패했다. 단독 모델 이미지의 `/app/result`는 통합할 때
`/opt/dta/result`로 이동하지만 worker의 cwd는 `/app`이므로 디렉터리 준비가 누락되었다.
통합 Dockerfile은 `/app/result`만 evidrug 소유로 생성하고 실제 비root 사용자로 임시 파일
생성·정리를 빌드 중 검증한다. 코드·checkpoint 디렉터리의 권한은 넓히지 않는다.

다른 분석이 실행 중이지 않을 때 worker만 교체하고 기존 입력으로 단독 진단한다.

```sh
docker compose -f compose.yaml -f compose.poc.yaml up -d --build --no-deps worker
bash scripts/diagnose-dta.sh 71c85585-3a7d-4a94-9f20-618b2aefe036
```

기존 모델 기반 이미지가 있는 환경에서는 `build-poc.sh` 전체 재실행이 필요 없다.
단독 진단은 LLM 호출·DB 쓰기가 없으며 원래 실패 분석의 상태를 변경하지 않는다.
수정 후 사용자 단독 진단의 실제 추론 성공은 아래 검증 상태에 기록했다.

### 모듈 안내

- orchestration/runtime.py → poc_runtime.py: opt-in 조립, profile, 모델 수명주기
- orchestration/fixed_tools.py → upstream.py: admission 연결과 저장 참조 검증
- admet/agent.py, dta/agent.py: 전문 결과와 원 도구 참조
- orchestration/reasoning.py: 단일 해석과 비용 보존
- decision/agent.py: projection, citation 검증, 최종 연구 판단
- Dockerfile.poc, compose.poc.yaml, scripts/build-poc.sh: 모델 배포

API/DB migration은 없다. 신규 결과도 기존 agent_runs.output_json envelope를 사용한다.
해석 실패 시에도 이미 저장된 원 도구 결과는 유지한다.

## 검증 상태

아래는 당시 검증 이력이며 최종 전체 성공 선언이 아니다. 이후 실실행:

- `d721280c-fb79-40c0-bfba-94379739e159`: continuation completed, 새 3,920토큰.
  원본 Target/ADMET 해시 및 DTA/Decision 참조 일치. DTA v1·Decision v3 실행.
- `086220de-1891-41c8-b6d8-e03865f8175a`: 일반 UI 경로에서 DTA v2 반영 확인.
  predicted_pkd=4.811026573181152를 저장했으나 `reasoning_citation_unknown`으로 해석 거부.
  Decision은 conditional_go, 전체 partial_failure, 네 단계 합계 7,968토큰.
  원문이 없어 정확한 미허용 인용 문자열은 확인 불가. 다음 수정은 인용 계약 안정화다.
- v2 진단·continuation·reasoning·runtime 관련 로컬 테스트 34개 통과 (LLM 호출 없음).

코드는 worker 이미지에 복사된다. 프론트만 재빌드하거나 모델 이미지만 빌드해도 실행 중인
worker 코드는 바뀌지 않는다. worker 변경 시 실행 중 분석이 없는지 확인한 후 위 PoC override로
worker를 재빌드·교체한다. 이전 결과의 버전과 상태는 변경하지 않는다.

2026-09-22 사용자 재검증: DTA 결과 폴더 권한 수정 후 기존 breast cancer/CDK4 입력의
단독 모델 호출이 passed. predicted_pkd=4.811026573181152, cold load=62.54초,
inference=0.67초, 모델 peak RSS=742.52 MiB, 전체=67.50초. LLM 호출·SQL 쓰기 0회.
이는 통합 worker 환경의 모델 단독 추론 확인이며 정상 provider/SQL/해석/Decision까지의
전체 분석 성공을 뜻하지 않는다. 기존 실패 분석의 상태는 변경하지 않았다.

Decision v3는 인용/판단 제약과 거부 사유를 명시하고, ADMET의 선택 범위와 실행 상태를
구분한다. 원본 context.is_partial은 전체 catalog 중 선택 요약이라는 뜻이므로 Decision에
그 이름으로 전달하지 않는다. selection_coverage와 execution_status를 분리하고 저장 원본은
유지한다. ADMET 자체 해석 입력도 선택 범위와 누락 endpoint를 구분한다.
2026-09-22 사용자 continuation 실행 `2d7348ff-b059-41f2-b497-58a05bf4598c`의
Decision v3는 ADMET 실행 완료와 49개 중 12개 선택 범위를 구분해 설명했다.
Decision은 completed, DTA는 예측 성공 후 해석 출력 검증 거부로 partial_failure였다.
새 토큰은 DTA 620 + Decision 3,015 = 3,635이며 Target/ADMET는 재사용했다.
따라서 Decision projection의 설명 수정은 확인했지만 새 ADMET 해석 입력은 live 미검증이다.
해석 거부 원인의 세분화와 DTA 오류 문구는 별도 #114로 수정했다. 과거 거부 원인은 미확정이다.

#110 분리 전 사용자 continuation 실행에서는 원본 Target/ADMET와 새 DTA/Decision을 연결해
completed를 확인했다. 새 토큰은 DTA 558 + Decision 3,090 = 3,648이다.
이는 네 단계를 새로 호출한 실행이 아니라 저장 결과 기반 연결 검증이다.
기존 Decision의 ADMET 부분 실패 오해는 위 v3 변경의 동기이며 과거 출력은 수정하지 않는다.

분리 브랜치의 fake provider/client 테스트는 전체 성공/부분 실패/잘못된 LLM 출력,
SQL 저장, 중복 전달, worker opt-in 조립과 자원 정리를 검증한다.
2026-09-22 분리 후 전체 271 passed / 3 skipped, Ruff/format/mypy와 shell 구문 검사 통과.
#114 병합을 반영한 PR 준비 시 전체 275 passed / 3 skipped 및 동일 정적 검사 통과.
키/API 호출이나 모델 다운로드 없이 실행하며 이번 분리 작업의 애플리케이션 LLM 호출은 0회다.
작업 환경에서 Docker socket 접근이 거부되어 변경 후 이미지 빌드 및 fresh 전체 live 실행은
직접 검증하지 못했다. 사용자 단독 진단과 continuation 성공을 fresh 전체 실행으로 혼동하지 않는다.

관련: #109,
#96,
#110.
