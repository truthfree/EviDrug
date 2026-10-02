# 도구 실행 상태와 lease 기반 장애 회수

## 목표와 범위

ADMET·DTA는 CPU 추론이 오래 걸리고 메모리 부족 등으로 worker가 종료될 수 있다.
실행 전 기록을 남겨 중복 재전달에 따른 비용을 막고, 종료 이후에도 미완료 작업을 식별한다.
포함 범위는 공통 SQL ledger, 서비스 연결, timeout/취소, 만료 회수다.
전체 agent orchestration, API·UI 변경, 자동 재시도, Render 배포·구독 변경은 제외한다.

## 계약과 관찰 가능한 동작

입력은 분석/run/request/tool call ID와 도구별 typed 입력이다. 정상 출력은 기존 도구 결과와
같으며 정상 계산 결과의 의미나 모델 구현을 변경하지 않는다.

- 실행 전에 `running`이 다른 DB session에도 보인다.
- 같은 ID가 실행 중이면 중복 추론을 시작하지 않는다.
- 결과와 `succeeded` 또는 `failed` 상태는 함께 저장한다.
- timeout/취소/저장 오류는 실패로 남기고 강제 종료는 lease 만료 후 실패로 회수한다.
- 회수 후 도착한 결과는 거부한다. 재시도에는 새 ID를 사용한다.
- 완료한 같은 ID는 기존 결과를 반환한다. 결과 없이 실패한 호출은 오류를 반환한다.

공개 HTTP API와 화면 계약은 변경하지 않는다. 전체 분석 상태 전이와 사용자 재시도 화면은
후속 orchestration·프론트엔드 작업에서 별도 정의한다.

## 보안·비용·운영 경계

기존 정규화 SQL 결과를 유지하고 ledger에는 입력 hash와 고정 오류 코드만 추가한다.
원본 예외·API key는 저장하지 않는다. DB 시간을 사용해 worker 시계 차이를 피한다.
기본 timeout 300초와 30초 여유 lease를 사용하며 갱신 heartbeat는 없다.
worker와 Beat가 실행되어야 주기 회수가 작동한다. 회수는 재계산이나 서버 재시작이 아니다.
환경당 Beat 하나를 운영하고 신규 migration 적용 후 서비스를 시작한다.

## 완료 조건과 검증 한계

별도 session에서 실행 전 commit, 중복 배제, 실패 기록, 만료 회수, 늦은 결과 차단,
새 ID 재시도 및 결과 저장 실패 rollback을 검증한다. 기존 ADMET·DTA 회귀 테스트도 유지한다.
로컬 SQLite 검증은 운영 PostgreSQL의 부하/잠금 검증을 대체하지 않는다.
Docker·Render 변경 없이 진행하며 실제 worker/Beat 운영 검증은 배포 단계에서 수행한다.

관련 백엔드: Issue #77.
프론트엔드 변경 없음. 구현/운영 안내: [공통 실행 계층](../../backend/src/evidrug_api/tool_execution/README.md).
