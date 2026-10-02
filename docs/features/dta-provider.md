# DTA 실행과 Render 자원 정책

## 목표와 이번 범위

DeepPurpose를 초기 CPU baseline으로 사용하고 MAMMAL·Boltz를 후속 provider 후보로 둔다.
provider 계약, 예상 가능한 장애 처리와 완료된 결과의 SQL 저장을 구현했다.
후속 #73에서 Python 3.10 DeepPurpose runtime과 Python 3.12 subprocess provider,
완료 결과 재사용·저장 service를 연결했다. #77에서 공통 실행 ledger와 lease 회수를 추가한다.
전체 orchestration 및 Render 배포는 후속 범위다.
기존 API 응답이나 분석 단계 실행은 이번 변경으로 달라지지 않는다.

## 입력과 결과

정규화 SMILES와 단백질 서열을 입력하고 provider·model·version·artifact 식별 정보,
score type·값·단위, 성공/이용 불가 상태와 소요 시간을 반환한다.
pKd와 pIC50-like, binding probability를 평균하거나 공통 confidence로 바꾸지 않는다.
`unavailable`은 데이터 공백이며 음성 결합 예측이 아니다.
Decision의 재평가 요청을 실행하는 권한과 budget 검증은 orchestration에 둔다.

## 합의한 배포 조건

현재 Free에서 먼저 시험하고, 자원 부족이 확인되면 사용자가 필요에 따라 `1c-2g`로
업그레이드할 수 있다. 현재 구독 변경 승인을 의미하지 않는다.
Free라는 이유만으로 provider를 영구 비활성화하거나 유료 서비스를 자동 생성하지 않는다.
모델 실행 위치와 계약을 분리해 인스턴스 변경 후에도 같은 입력·결과 계약을 사용한다.

DeepPurpose smoke의 peak RSS는 747.4 MiB였으므로 512MB Free에서는 OOM 가능성이 높다.
테스트 중 일시적인 API 중단을 감수한다. 1c-2g도 실측 전에는 실행 보장이 아니다.
동일 인스턴스에서 subprocess를 나누더라도 메모리 제한을 공유한다.
별도 서비스로 배치하면 자원과 요금도 별도이므로 추후 배포 선택에 명시한다.

## 실제 배포 전 연결할 기능

- 모델 runtime은 우선 동시 실행 1개, CPU thread 1개로 검증한다.
- 작업 시작 상태를 DB에 먼저 commit하고 worker 종료 후 lease 만료로 회수한다.
- 강제 종료된 작업은 무한 자동 재시도하지 않고 사용자 재시도를 허용한다.
- import·모델 load·추론 시간과 peak RSS를 측정한다.
- 모델을 서버 시작 시 무조건 로드해 반복 재시작하는 상황을 피한다.
- OOM 진단은 platform 로그·event와 결합한다. SIGKILL을 Python except로 잡을 수 없다.

현재 runtime은 CPU thread 1개, provider 객체별 직렬 실행, 첫 요청 lazy load와 모델 재사용을
구현한다. timeout·취소·프로세스 종료 후 다음 요청에서 runtime을 재시작할 수 있다.
공통 실행 ledger에 시작 기록을 남기고 강제 종료 후 만료 회수를 지원한다.
주기 회수에는 별도 worker/Beat 운영이 필요하며 서버 재시작과 전체 분석 상태 연결은 별도다.
2026-09-18 사용자 실행으로 로컬 Docker 1 CPU/2GB provider 통합 검증이 통과했다.
첫 호출 71.835초, 모델 재사용 호출 0.486초, 모델 프로세스 peak RSS 747.109 MiB였고,
SQLite round-trip과 완료 호출 재전달을 확인했다. Render/Free 및 운영 PostgreSQL 검증은 별도다.

## 완료 조건과 검증

이번 계약 단계는 실패에 가짜 score를 생성하지 않고, SQL round-trip과 멱등성을 검증한다.
운영 PostgreSQL 및 Render 모델 실행 검증은 별도로 수행한다.

관련: Issue #71,
선행 smoke PR #70,
[구현 안내](../../backend/src/evidrug_api/dta/README.md).
