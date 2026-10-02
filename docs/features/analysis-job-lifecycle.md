# 분석 작업 수명주기 기능 명세

## 1. 배경과 사용자 목표

사용자는 질환, 선택적 타깃과 화합물을 확인한 뒤 오래 걸리는 분석을 시작하고 페이지를
떠나더라도 같은 작업의 상태를 다시 확인할 수 있어야 한다. Redis나 worker의 일시 상태가
아니라 PostgreSQL에 저장된 분석 ID와 상태를 기준으로 삼는다.

## 2. 포함 및 제외 범위

### 포함

- 검증된 입력으로 영속 분석 작업 생성
- 세션 범위 중복 제출 방지
- 분석 전체와 초기 단계 상태 저장
- Celery에 `analysis_id`만 전달
- queue 전달 실패 보존
- 인증된 상태 조회

### 제외

- Target, DTA, ADMET 및 Decision Agent 실행
- 단계 상태를 변경하는 orchestration
- EvidenceClaim과 최종 결과 저장
- 프런트엔드 실행, polling 및 결과 화면
- 스테이징 PostgreSQL과 worker 배포

## 3. 정상 사용자 흐름

1. 사용자가 앞 화면에서 질환, 타깃 방식과 SMILES를 확인한다.
2. 프런트엔드는 한 번의 제출 시도 동안 유지되는 `Idempotency-Key`와 함께 분석 생성을 요청한다.
3. API는 입력을 다시 검증하고 `queued` 작업과 초기 단계를 저장한다.
4. API는 작업 ID와 현재 상태를 즉시 반환한다.
5. 프런트엔드는 작업 ID로 상태를 조회한다.
6. 새로고침하거나 같은 결과 URL로 돌아와도 PostgreSQL에 저장된 상태를 확인한다.

## 4. 입력과 출력

입력은 `POST /api/v1/analysis-inputs/validate`와 같은 질환, 타깃 방식과 SMILES 계약을
사용한다. 서버가 만든 canonical SMILES와 사용자의 원본 SMILES를 모두 저장한다.

응답에는 다음 정보가 포함된다.

- `analysis_id`
- 전체 `status`
- 확정 입력과 canonical SMILES
- 단계별 이름, 상태와 표시 순서
- 공개 가능한 상태 event와 사유 코드
- 오류 코드
- 생성 및 마지막 갱신 시각

## 5. 상태와 오류

전체 상태는 `queued`, `running`, `completed`, `partial_failure`, `failed`를 사용한다. 단계는
`pending`, `running`, `completed`, `failed`, `skipped`를 사용한다.

| 상황 | 응답 또는 저장 동작 |
| :--- | :--- |
| 인증 없음 | `401 invalid_session` |
| 허용되지 않은 생성 Origin | `403 origin_not_allowed` |
| 잘못된 SMILES | `422 invalid_smiles`, 작업을 만들지 않음 |
| 잘못된 idempotency key | `422 invalid_idempotency_key` |
| 없는 분석 ID | `404 analysis_not_found` |
| queue 전달 실패 | 분석을 `failed`, 오류를 `queue_unavailable`로 저장 |

일시적인 상태 조회 실패를 분석 실패로 바꾸지 않는다. queue 전달 실패는 작업이 저장된 뒤
발생하므로 생성 응답에 실패 상태와 분석 ID를 함께 반환해 사후 조회가 가능하게 한다.

## 6. API 계약

### 분석 생성

```http
POST /api/v1/analyses
Idempotency-Key: analysis-request-001
Content-Type: application/json
```

성공 및 영속 queue 실패 모두 작업 레코드를 나타내는 `202` 응답을 사용한다. 같은 세션과
key의 재요청은 기존 작업을 반환한다. 다른 key는 동일 입력이어도 새 분석 의도로 간주한다.

### 상태 조회

```http
GET /api/v1/analyses/{analysis_id}
```

현재 POC는 개별 계정이 없으므로 유효한 공용 세션과 추측하기 어려운 분석 ID를 가진 사용자가
조회할 수 있다. 사용자 계정 도입 시 소유권 검사를 추가한다.

## 7. 보안과 비용 제약

- 생성은 인증과 허용 Origin을 모두 검사한다.
- 조회도 분석 ID만으로 접근할 수 없고 인증을 요구한다.
- 세션, 접근 코드와 API key 원문을 저장하거나 로그에 기록하지 않는다.
- `Idempotency-Key`는 세션 fingerprint와 함께 unique하다.
- 분석 생성은 단일 프로세스 기준 기본 60초당 5회로 제한한다.
- worker 메시지에는 SMILES나 결과 대신 `analysis_id`만 넣는다.
- GPT 연결 전 일일 영속 예산과 동시 분석 상한을 별도로 추가한다.
- DB commit과 broker 전달 사이의 불확실성은 후속 transactional outbox와 worker 멱등성으로 보강한다.

## 8. 완료 조건

- 유효한 생성 요청이 작업, 네 단계와 생성 event를 원자적으로 저장한다.
- 중복 요청이 새 작업 또는 queue message를 만들지 않는다.
- queue 실패가 조회 가능한 실패 상태로 남는다.
- 생성과 조회의 인증·Origin·입력 오류가 구분된다.
- migration SQL, Ruff, format, mypy와 전체 backend test를 통과한다.

## 9. 관련 Issue

- Backend: [#53](https://github.com/truthfree/Dacon2026/issues/53)
- Evaluation architecture: [#34](https://github.com/truthfree/Dacon2026/issues/34)
- Frontend 실행·진행 화면: 후속 Issue
