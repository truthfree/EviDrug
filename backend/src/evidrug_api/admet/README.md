# ADMET tool adapter

## PoC Agent 연결

`agent.py`의 `AdmetAgent.execute`는 고정 binding/admission을 통해 아래 실행 계층을 사용한다.
원 예측 전체를 SQL에 한 번 저장하고 `POC_ENDPOINTS` 16개만 context로 투영한다.
ADME 12개와 독성 4개를 각각의 LLM 호출로 해석해 `AdmetAgentResult.adme`와
`.toxicity`에 선택 endpoint, 해석/오류, 사용량을 별도로 보존한다. 전체 Agent 사용량은
두 호출의 합계다. 기존 `interpretation`은 구형 조회 화면과 저장 결과 호환을 위한
결정적 연결 문장이며 새 Decision 입력에는 중복 전달하지 않는다. 구형 저장 결과는
두 영역 필드가 없어도 계속 읽을 수 있다. 선택 정책 버전은
`poc-admet-domain-endpoints-v2`, Agent 구현 버전은 `admet-agent-v3`이다.
ADME 기본 지표는 흡수 4개(용해도·Caco-2·HIA·경구 생체이용률), 물성·분포
4개(logP·TPSA·분자량·혈장단백결합률), 제거 경향 2개(간세포·마이크로솜
청소율)다. CYP3A4·CYP2D6 억제 2개는 별도의 병용약물 상호작용 신호다.
투여 경로 입력이 없으므로 경구 지표는 경구 개발 시나리오로만 해석한다.
청소율 모델은 실제 전신 청소율이나 노출량 계산에 사용하지 않으며, 선택하지 않은
분포용적·반감기·직접 배설도 추정하지 않는다.
도구 실패는 failed, 해석/선택 근거 부족은 원 관측을 유지한 partial_failure다.
전문 해석 prompt v4는 응답을 단일 JSON 객체로 요구한다. 응답 전체가 하나의 Markdown
JSON 코드블록으로 감싸진 경우에만 포장을 제거한 뒤 기존 schema와 근거 ID를 그대로
검증하며, 복구 사실을 `reasoning_json_fence_removed` 경고로 저장한다. 설명문·복수 객체·
실제 JSON 문법 오류는 보정하지 않고 실패로 남긴다. API를 자동 재호출하지 않으므로
모델 비용과 `external_requests`는 늘지 않는다. 원 응답을 저장하지 않는 정책상 과거
`reasoning_json_invalid`의 정확한 문법 형태를 사후 판별하거나 이 수정으로 재현 없이
완치됐다고 주장할 수 없다.
전용 worker와 미검증 범위는 [PoC 명세](../../../../docs/features/poc-full-execution.md)를 따른다.
아래 provider smoke 기록은 이번 통합 이미지 검증과 구분한다.

ADMET-AI provider의 큰 JSON report를 모델 버전에 고정된 manifest와 실행별 prediction 행으로
분리한다. 이 계층은 예측값을 과학적으로 해석하지 않으며, 고정 실행과 Agents SDK 기반
ADMET Agent가 같은 입력·출력 계약을 사용하게 한다.

## 데이터 경계

### 독성 확인시험 우선순위 (#296)

`toxicity.py`의 `calculate_toxicity_axes`는 DILI·hERG·AMES 세 축만 계산한다.
반올림 전 값 >=0.5와 승인약 집단 percentile >=90을 모두 충족하면 `priority_check`,
관측이 있으나 미충족이면 `not_priority`, 관측/선택 값이 없으면 `missing`이다.
LD50에는 축 상태를 부여하지 않는다. 이는 확인시험 선택 정책이지 임상 독성 확률이나
안전성 판정이 아니며, `not_priority`는 안전/무독성을 뜻하지 않는다.

Agent는 동일 계산 결과를 `toxicity_axes`로 저장하고 독성 해석 입력의 `model_context`에
전달한다. 각 축은 endpoint·원값·승인약 백분위·원 tool call ID와 정책 버전
`toxicity-priority-policy-v1`을 포함한다. 해석 실패의 partial_failure에서도 관측과 축을
보존하며 공개 결과 API에도 그대로 전달한다. 과거 결과의 축 기본값은 None이며 조회 때
새 정책으로 소급 판정하지 않는다. SQL 원 관측 저장 구조와 migration은 변경하지 않는다.

독성 프롬프트 `toxicity-interpretation-v3`는 공급된 상태를 사용하고 자체 축 순위 계산이나
안전 선언을 금지한다. ADME `adme-interpretation-v4`는 benchmark 요구만 제거하고
임상 노출·전신 청소율 추정 금지는 유지한다. #301 strict JSON 계약도 그대로 유지한다.
CToxPred2 negative 결과가 이 함수를 덮어쓰거나 축을 해제하는 경로는 추가하지 않는다.
Decision 공유 계산/채널 연결은 #297, 구조화 판정은 #298, 화면·다운로드 연결은 #299다.
실 모델 호출·전체 화면 검증 완료와 이 PR의 fake/SQLite 검증은 구분한다.

```text
ADMET-AI provider report
        │
        ▼
AdmetToolAdapter
        ├── AdmetModelManifest  ── 모델·endpoint·DrugBank 기준 집단별 1회 저장
        └── AdmetToolResult     ── 분석 실행마다 저장
              └── endpoint_id, value, drugbank_approved_percentile
```

`category`, `name`, `units`, dataset 정보와 benchmark metric은 prediction 행마다 반복하지
않는다. `AdmetToolResult.manifest_sha256`으로 stable manifest를 참조한다. SQL에서는 다음
테이블로 정규화해 저장한다.

| 테이블 | 주요 key | 내용 |
| :--- | :--- | :--- |
| `admet_model_manifests` | `id`, unique `manifest_sha256` | tool/model/reference 식별자와 hash |
| `admet_model_artifacts` | `manifest_id`, `relative_path` | ensemble 모델 파일 hash |
| `admet_endpoints` | `id`, unique (`manifest_id`, `endpoint_id`) | endpoint metadata column |
| `admet_manifest_limitations` | `manifest_id`, `position` | 순서가 있는 해석 제한사항 |
| `admet_tool_executions` | `tool_call_id` | analysis·agent run·manifest와 입력 SMILES |
| `admet_predictions` | `tool_call_id`, `endpoint_record_id` | 실행별 값과 DrugBank percentile |

`AdmetRepository.save()`는 manifest 조회 또는 생성, 실행, 전체 prediction을 한 transaction으로
저장한다. 같은 `tool_call_id`와 같은 내용의 재요청은 기존 행을 반환하고, 식별자는 같지만
내용이 다르면 충돌로 거부한다. prediction의 복합 foreign key는 실행과 endpoint가 반드시
동일한 manifest에 속하도록 강제한다.

분석 행을 삭제하면 그 분석의 execution과 prediction은 cascade 삭제하지만, 여러 실행이
공유할 수 있는 manifest와 endpoint catalog는 보존한다. 반대로 참조 중인 manifest의 삭제는
제한한다. `run_id`는 아직 agent run 테이블이 없으므로 UUID로만 저장하며, 해당 테이블을
도입할 때 foreign key migration을 추가한다.

이 Pydantic 모델은 검증·저장 계약이지 LLM prompt 직렬화 형식이 아니다. 이후 context
연결은 아래 `AdmetContextBuilder`의 선택 projection을 사용한다.
전체 모델을 매 turn JSON으로 dump하지 않는다.

percentile은 ADMET-AI가 동봉 DrugBank approved 기준 집단으로 계산해 반환한 원본 값을
연결할 뿐 confidence로 변환하거나 다시 계산하지 않는다. 다른 기준 집단 비교는 별도 도구와
명시적인 reference identifier가 필요한 후속 기능이다.

## 검증 정책

- endpoint metadata ID와 prediction ID는 정확히 일치해야 한다.
- 모든 endpoint에 원본 prediction과 DrugBank percentile이 하나씩 있어야 한다.
- classification 값은 0~1, percentile은 0~100 범위여야 한다.
- bool, 비수치, NaN과 무한대 prediction은 거부한다.
- metadata의 빈 문자열과 `-`는 `None`, 범위의 `±inf`는 unbounded flag로 보존한다.
- 모델 파일, endpoint CSV와 DrugBank reference의 SHA-256을 manifest에 보존한다.

실제 ADMET-AI 실행은 현재 격리된 Docker provider의 책임이다. adapter는 Docker socket,
환경변수 또는 모델 파일에 직접 접근하지 않고 `AdmetReportProvider`가 반환한 report만
검증한다. orchestration의 승인·예산·재시도와 ADMET Agent의 위험 해석도 이 모듈 밖의
책임이다.

## 실제 실행과 완료 결과 재사용

읽는 순서는 `service.py → adapter.py → provider.py → repository.py`다.
`AdmetExecutionService.execute`는 분석 SMILES를 확인하고 provider 결과를 기존 정규화·SQL 저장
경로로 전달한다. 완료된 같은 tool call은 ID와 입력을 검증한 뒤 저장 결과를 그대로 반환한다.
동시에 도착한 같은 ID의 호출은 공통 `ToolExecutionRunner`가 차단한다.
추론 전에 running을 commit하고 결과와 완료 상태를 원자적으로 저장한다.

`AdmetSubprocessProvider`는 고정 argv로 별도 Python 환경을 실행하며 객체당 하나의 모델
프로세스를 유지한다. 같은 event loop에서 재사용하고 종료 시 `await provider.aclose()`로 닫는다.
예: `AdmetSubprocessProvider(("/opt/admet/.venv/bin/python", "/opt/admet/runtime.py"))`.
경로는 배포 예시이며 현재 Render에 설치된 경로가 아니다. argv는 LLM/HTTP 입력으로 받지 않는다.

전체 호출(잠금 대기 포함)에 기본 300초 timeout을 적용한다. 취소·timeout·잘못된 protocol·
프로세스 종료 시 자식을 회수하고 다음 호출에서 새로 시작한다. 기다리는 호출만 취소되면
이미 실행 중인 요청은 유지된다. `AdmetProviderUnavailable.code`로 timeout과 실행 장애를
구분한다. report 계약 위반은 `AdmetReportError`, SQL 실패는 repository 예외로 전파한다.

ADMET의 기존 SQL 계약은 성공 결과만 저장한다. 실패 예외를 성공 결과나 score 0으로 변환하지
않는다. 실패 이력은 공통 `tool_executions`에 고정 오류 코드로 저장한다.
DTA의 unavailable 결과와 달리 ADMET 도메인 결과 행은 성공 시에만 생성한다.
lease 회수와 재시도 규칙은 [공통 실행 계층](../tool_execution/README.md)을 따른다.

`experiments/admet-smoke/runtime.py`는 첫 요청에 모델을 로드한다. CPU thread 1개,
`num_workers=0`, molecule cache 비활성화로 실행한다. 잠금 파일로 설치한 패키지의 모델·
참조 데이터 hash를 이미지 빌드 시 기록하고 runtime에서 모델 로드 전에 대조한다.
이는 독립적인 upstream 서명이 아니라 빌드된 이미지 내 파일 변경 탐지다.
runtime report의 고정 metadata는 pipe로 전달되며 LLM prompt에는 들어가지 않는다.
SQL에서는 기존 manifest/endpoint를 재사용한다.

metrics는 로그 `admet_runtime` extra field와 통합 보고서에 남긴다. SQL 자원 이력은 아직 없다.
실제 API/Celery 연결 및 Render 이미지 변경은 포함하지 않는다. 동일 인스턴스에 두 모델을
상주시키면 메모리는 합산되므로 DTA 단독 2GB 검증을 ADMET+DTA 동시 실행 보장으로 해석하지 않는다.

자동 검증: backend에서 `uv run --no-env-file pytest tests/test_admet_provider.py tests/test_admet_repository.py`.
실제 사용자 검증: 루트에서 `bash experiments/admet-smoke/run-provider.sh`.
2026-09-18 사용자 실행으로 로컬 1 CPU/2GB에서 49개 endpoint의 SQL round-trip과
cold→warm 모델 재사용이 통과했다. warm 추론 0.576초, 모델 프로세스 peak RSS 763.949 MiB였다.
관련: Issue #75.

## 저장 결과 조회·context 구성

`context.py`의 `AdmetContextBuilder.build(analysis_id=..., query=...)`는 SQL에서
선택한 endpoint만 읽는다. 입력은 `AdmetContextQuery(source_tool_call_id=..., endpoint_ids=...)`,
출력은 versioned `AdmetContext`다. 모델 실행과 DB 쓰기를 하지 않는다.

| 진입점 | 역할 | 오류 |
| :--- | :--- | :--- |
| `AdmetContextQuery` | 원 실행 ID와 1~20개 endpoint 선택 | 빈/중복/과다 선택 검증 실패 |
| `AdmetContextBuilder.build` | 인증된 분석 범위의 SQL 결과 → columnar context | `AdmetContextUnavailable`, DB/계약 검증 오류 |
| `AdmetContext` | 고정 columns·typed rows·버전·출처·제한 보존 | 비정상 수치·열 순서·부분 선택 표시 거부 |

`context.model_dump(mode="json")` 또는 `model_dump_json()`은 열 이름을 한 번만 쓰고
각 행을 배열로 직렬화한다. 필요한 endpoint는 호출자가 명시하며 자동 위험 선별은 하지 않는다.
원 값을 반올림하거나 percentile을 재계산하지 않는다. projection version은 `admet-context-v1`이다.
출처/제한 포함 응답이 32 KiB를 넘으면 실패한다. 이는 토큰 상한과 같지 않다.

`analysis_id`는 서버에서 소유권 확인 후 전달한다. 이 builder는 인증 API가 아니며
Agent가 직접 호출하는 등록 도구도 아니다. 나중에 Agent용 조회 도구를 만들 때는 별도
binding·admission·조회 이력 계약을 연결해야 한다. 계산 runner를 우회하는 예외가 아니다.
테스트: `uv run --no-env-file pytest tests/test_admet_context.py`.
정상 경로는 4개 SELECT를 사용하며 session transaction 종료는 호출자가 맡는다.
상세 오류·선택 정책·미구현 범위는 [기능 명세](../../../../docs/features/admet-result-context.md)를 따른다.

관련: Issue #64,
Issue #66,
[ADMET baseline](../../../../docs/features/admet-baseline.md),
[OpenAI tools](https://developers.openai.com/api/docs/guides/tools)
