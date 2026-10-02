# 분석 작업 수명주기

## 목적과 책임

검증된 질환, 타깃 방식과 SMILES를 PostgreSQL에 분석 작업으로 저장하고 Celery에는 작업 ID만
전달한다. 작업 전체와 단계의 현재 상태, 생성·전달 실패 event를 조회할 수 있게 보존한다.
실제 Agent 실행과 단계 상태 전이는 [orchestration](../orchestration/README.md)이 담당한다.

## 폴더 구조

```text
analysis_jobs/
├── models.py        API 상태와 응답 schema
├── tables.py        Analysis, AnalysisStage, AnalysisEvent ORM 모델
├── repository.py    멱등 생성, 조회와 상태 event 저장
├── service.py       SMILES 검증, 저장과 queue 전달 조정
├── dispatcher.py    analysis_id만 전달하는 Celery adapter
├── dependencies.py  앱에 구성된 dispatcher 접근
└── router.py        생성 및 상태 조회 API
```

## API

| Method | 경로 | 역할 | 성공 |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/v1/analyses` | 분석 작업을 저장하고 queue에 등록 | `202`와 현재 상태 |
| `GET` | `/api/v1/analyses` | 같은 브라우저 또는 현재 세션의 최신 입력 요약 20개 | `200`과 `items` |
| `GET` | `/api/v1/analyses/{analysis_id}` | PostgreSQL에서 작업·단계 상태 조회 | `200`과 현재 상태 |
| `GET` | `/api/v1/analyses/{analysis_id}/results` | 저장 Agent 호출 순서·요청 관계 및 전문 결과 조회 | `200`과 호출별 결과/가용성 |

생성 요청은 `Idempotency-Key` header가 필요하다. 8~128자의 영문·숫자와 `._:-`만 허용하며,
같은 인증 세션과 key의 반복 요청은 최초 작업을 반환하고 다시 queue에 넣지 않는다. 세션
원문은 저장하지 않고 SHA-256 fingerprint를 멱등 범위와 조회 소유권 검사에 사용한다.
GET은 생성 당시 세션 또는 유효한 동일 브라우저 ID를 허용한다. 다른 방문자·없는 분석·
CLI replay의 상세 조회는 404로 구분 없이 거부한다. 목록에는 입력 요약만 포함하고
실행 결과는 포함하지 않는다. 새 브라우저 ID 도입 전에 세션으로만 저장한 분석은
현재 세션에서는 보이지만, 만료된 세션의 과거 기록을 새 방문자에게 임의 귀속하지 않는다.

생성 API는 [분석 입력 검증](../analysis_input/README.md)과 같은 요청을 받고 SMILES를 다시
검증한다. 클라이언트에서 앞 단계 검증을 마쳤더라도 서버가 canonical SMILES를 신뢰하지
않고 직접 계산해 원본과 함께 보존한다.

## 상태와 데이터 흐름

1. 인증, Origin과 `Idempotency-Key`를 확인한다.
2. RDKit으로 SMILES를 canonicalize한다.
3. `queued` 분석, 네 개의 `pending` 단계와 `analysis_created` event를 한 transaction에 저장한다.
4. commit 후 Celery에 `analysis_id` 문자열 하나만 전달한다.
5. broker 전달이 실패하면 작업을 `failed`로 바꾸고 `queue_dispatch_failed` event를 추가한다.
6. 조회 API는 queue가 아니라 PostgreSQL의 상태를 반환한다.
7. 완료된 Target run이 있으면 protein sequence를 제외한 association, causal support,
   tractability와 therapeutic direction을 `target_prioritization` 요약으로 함께 반환한다.
   causal 확장 이전 Target v2 결과는 causal support를 `unknown`으로 투영한다.

DB commit과 broker 전달은 하나의 transaction이 아니다. broker가 메시지를 받았지만 응답만
실패한 드문 경우에는 실패 상태와 실제 수신이 엇갈릴 수 있다. 다중 worker와 자동 복구를
도입할 때 transactional outbox 및 worker 멱등 실행으로 이 경계를 보강한다.

초기 단계 순서는 Target Hypothesis, ADMET, DTA, Decision이다. `position`은 표시 순서이며
실행이 직렬이라는 뜻은 아니다. orchestration은 Target Hypothesis와 ADMET을 병렬로 실행하고,
DTA는 표적 입력이 확보된 뒤 실행한다.

## 마이그레이션

PostgreSQL 실행 전 다음 명령으로 schema를 적용한다.

```sh
uv run alembic upgrade head
```

루트 Docker Compose는 `migrate` service가 같은 명령을 먼저 실행하며 성공한 뒤 API와 worker를
시작한다.

초기 migration은 `migrations/versions/0001_create_analysis_jobs.py`다. ORM 모델을 변경하면
자동 생성 결과를 그대로 신뢰하지 말고 PostgreSQL SQL과 downgrade 순서를 검토한다.

## 주요 수정 지점

- 상태나 응답 필드: `models.py`
- Target 결과의 공개 projection: `repository.py`, `service.py`
- 전문 결과 공개 DTO: `result_models.py`; 검증·투영: `results.py`
- DTA 후보의 `model_runs`는 모델별 상태·관측·도구 호출 ID를 분리해 노출한다. 기존
  후보 대표 필드는 첫 성공 모델의 값으로 유지하며, 과거 결과의 모델별 목록은 비어 있다.
- `calls`는 Agent별 `call_number`와 요청·응답 run 참조를 보존한다. 기존 화면용
  `admet`·`dta`·`decision`은 유지하지만 두 번째 ADMET 결과는 별도 cardiac 슬롯이 아니라
  `calls`의 ADMET 2번 호출에서 조회한다.
- 조회 소유권: `service.py:read_owned_analysis`; 최신 attempt 조회: `repository.py`
- 브라우저 목록: `repository.py:list_recent`, `service.py:list_recent_analyses`
- 테이블과 관계: `tables.py` 및 새 Alembic migration
- 상태 전이와 event 저장: `repository.py`
- queue 제공자 교체: `dispatcher.py`
- 실행 순서와 Agent adapter 연결: `orchestration/service.py`, `contracts.py`
- 생성 요청량: `config.py`의 `api_analysis_limit`

기본 worker는 orchestration과 실제 Target adapter를 시작한다. 전용 PoC worker는
ADMET/DTA/Decision도 연결한다. 키가 없거나 기본 환경의 미구성 단계는
`agent_not_configured`로 기록하며 가짜 결과를 만들지 않는다.
[PoC 실행 안내](../../../../docs/features/poc-full-execution.md)를 참고한다.

## 테스트

```sh
uv run --no-env-file pytest tests/test_analysis_jobs.py tests/test_specialist_results.py
uv run alembic upgrade head --sql
```

테스트는 SQLite 비동기 저장소와 기록용 dispatcher를 사용해 생성, 멱등성, queue 실패,
인증, Origin과 오류 응답을 검증한다. 실제 PostgreSQL migration 적용과 Redis/Celery 전달은
통합 환경에서 별도로 확인한다.

## 관련 문서

- [분석 작업 수명주기 기능 명세](../../../../docs/features/analysis-job-lifecycle.md)
- [브라우저별 최근 분석 기록](../../../../docs/features/browser-analysis-history.md)
- [전문 결과 조회 계약](../../../../docs/features/specialist-results-api.md): 실행 상태와 선택 범위,
  부분 실패/과거 출력, 원본 참조와 비용 의미. 조회는 SQL 읽기 전용이며 추론을 실행하지 않는다.
- [비동기 분석 실행 구조](../../../../docs/spec.md#6-분석-실행-구조)
