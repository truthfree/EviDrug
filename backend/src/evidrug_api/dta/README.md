# DTA provider 계약과 저장

## Shortlist Agent (#96)

`agent.py`는 검증된 Target 후보별 예측을 기존 admission/runner로 연결하고 결과를 집계한다.
읽는 순서와 검증 범위는 [DTA shortlist Agent](../../../../docs/features/dta-shortlist-agent.md)를 따른다.
운영 worker 등록과 Decision 연결은 별도 #109 범위다.

## 책임과 흐름

`DtaExecutionService.execute → DtaToolAdapter.execute → DeepPurposeProvider.predict → DtaRepository.save`
순으로 읽는다. `DtaArguments`와 `DtaResult`가 각 경계의 입력·출력을 정의한다.
`contracts.py`는 입력과 score 의미, `adapter.py`는 비동기 실행 경계,
`repository.py`와 `tables.py`는 SQL 저장을 담당한다.
`deeppurpose.py`는 별도 Python 3.10 프로세스와 pipe로 통신하며, `service.py`는 완료 결과의
재전달 시 기존 결과를 재사용한다. 실제 모델 코드는 `experiments/deeppurpose-dta-smoke/runtime.py`다.

| 진입점 | 역할·입출력 | 오류 |
| :--- | :--- | :--- |
| `DtaProvider.predict` | 정규화 SMILES와 표준 아미노산 서열 → typed 관측 tuple | 예상 가능한 장애는 `DtaProviderUnavailable` |
| `DtaToolAdapter.execute` | provider 호출 → 성공 또는 `unavailable` 결과 | 취소·구현 오류는 전파 |
| `DtaRepository.save` | 분석/run/request/tool call ID와 결과 → 새 저장 여부 | 다른 내용의 ID 재사용은 conflict |
| `DtaRepository.load` | tool call ID → 원 결과 또는 None | 저장된 서열 hash 불일치 거부 |
| `DeepPurposeProvider.predict/aclose` | 모델 프로세스 재사용·직렬 호출·종료 | 프로세스 종료, protocol 불일치 → unavailable |
| `DtaExecutionService.execute` | 완료 결과 재사용 또는 실행 후 SQL 저장 | 입력/ID 충돌, SQL 오류는 전파 |

## SQL 저장

- `dta_models`: provider·model ID·version·artifact hash를 한 번 저장한다.
- `dta_executions`: 분석과 실행 ID, 실제 SMILES·서열·서열 hash, 상태와 소요 시간을 저장한다.
- `dta_observations`: score type·value·unit을 column으로 저장한다.

원 JSON 덩어리를 반복 저장하지 않는다. model key는 metadata 전체의 canonical JSON hash다.
provider 버전은 반드시 실제 모델 revision을 식별해야 한다. DeepPurpose는 고정 checkpoint
archive hash를 artifact 식별자로 사용한다. 원 report 보관 및 LLM용 요약은 후속 실행 연동의 책임이다.
`predicted_pkd`, `pic50_like`, `binding_probability`는 개별 관측이며 합산하지 않는다.
서열은 20종 표준 아미노산 대문자만 허용한다. X/B/Z/U/O 등은 자동 치환하지 않고 거부한다.

## BindingDB 두 모델

| tool ID | model ID | DeepPurpose | checkpoint archive SHA-256 |
| :--- | :--- | :--- | :--- |
| `dta` | `CNN_CNN_BindingDB` | `0.1.5` | `1f5c62863303d5057566b3b29be24e31cc138b361dfb8b085b53c8169d8c6829` |
| `dta_mpnn_cnn_bindingdb` | `MPNN_CNN_BindingDB` | `0.1.5` | `655119c3896a773a8ccf0e711ad263a3bfbcd0bab7ea5085cccb12e13908bc0c` |

두 모델은 같은 BindingDB Kd 학습 계보의 다른 drug encoder 관측이다. 독립 실험 근거로
세거나 평균한 대표 pKd를 만들지 않는다. `DtaCandidateResult.model_runs`가 모델별
tool call·model metadata·원 관측·실패를 보존하며 결과 API의 후보별 `model_runs`에도
각각 투영한다. 기존 `model`/`observations`/`tool_call_id`는 첫 성공 관측을 가리키는
하위 호환 필드이며 두 값의 합성값이 아니다. 두 모델 도입 전 저장 결과에는
`model_runs`가 없을 수 있으며, 읽기 시 빈 목록과 기존 대표 필드를 유지한다.

## 실험근거 관계 판정과 조건부 PubChem

`evidence.py`는 PubChem에서 받아야 할 assay 결과와 관계 판정을 정의한다. 과거 저장된
BindingDB·ChEMBL assay 결과는 조회 호환성을 위해 읽을 수 있지만 새 분석에서는 조회하지 않는다.
정량 근거는 endpoint, 값, 단위와 원 레코드를 보존한다. Active/Inactive 같은 정성 결과는
별도 종류로 저장하며 정량 지지·충돌 판정에 사용하지 않는다.

분석 요청에 `potency_criterion`이 명시된 경우에만 두 모델과 직접 비교 가능한 실험값을
criterion의 같은 판단 영역으로 투영한다. criterion이 없으면 모델 점수 차이가 있어도
`DECISION_RELEVANT_DISAGREEMENT`를 만들지 않는다. 단위는 pM/nM/uM/mM/M 사이에서만
명시적으로 환산하며 endpoint가 다르면 비교하지 않는다.

DTA Agent는 Kd potency criterion의 기준선을 두 예측이 서로 다르게 넘는
`split_across_reference`일 때만 후보별 PubChem BioAssay를 최대 한 번 요청한다.
같은 영역·기준 없음·비-Kd 기준·모델 부족은 조회 조건이 아니다. 기준값과 같으면 이상
영역에 포함한다. `same_region_meets_reference`, `same_region_below_reference`,
`split_across_reference`, `insufficient_model_results`를 과거 관계 enum과 별도로 저장한다.

`pubchem_requested`는 정책상 요청 필요, `pubchem_executed`는 실행 시도 여부다.
provider 비활성·예산 소진·실행 실패는 각각 `provider_disabled`, `tool_budget_exhausted`,
`execution_failed`로 보존한다. 실패한 시도도 실행 이력이며 성공이나 근거 공백 해소가 아니다.
실측 추가 뒤에도 예측 영역과 요청 이력은 유지한다.

`experimental_binding_support`는 중복을 제거한 같은 표적의 비교 가능한 정량 Kd 기록이
하나 이상 있고 모두 입력 기준을 충족할 때만 true다. IC50·Ki·정성 결과와 예측 모델 점수는
실측 결합 지지에 포함하지 않는다. 이 상태는 모델 일치 및 조회 성공과 독립적이다.

새 DTA snapshot의 `assay_policy_version`은 `pubchem-recall-v1`이며 과거 필드 없는 결과는
`legacy`로 읽는다. 공개 요약과 snapshot replay에서도 버전을 유지한다. 새 Decision 생성은
legacy 또는 BindingDB·ChEMBL 실험 출처를 `decision_dta_assay_policy_mismatch`로 거부한다.
과거 결과 조회와 replay 자체는 허용하며 실 API fallback을 실행하지 않는다.

이 PR은 DTA 정책·공개 필드·신규 Decision 사용 관문만 변경한다. Decision v5.7 입력/출력과
독성 축 정책은 후속 #296–#298이며 전환 중간에 통합 계약이 완성됐다고 배포하지 않는다.

`assay_providers.py`는 PubChem PUG REST를 구현한다. PubChem은 SMILES를 단일 CID로 해석한 뒤
assay summary의 target accession이 정확히 같은 행만
채택한다. `EVIDRUG_DTA_ASSAY_PROVIDERS_ENABLED=false`가 기본값이며 승인된 smoke 전에는 운영
profile에서 켜지 않는다.

provider는 숨은 retry나 상호 fallback을 하지 않는다. HTTP timeout, rate limit, 5xx와 schema
오류를 고정 코드로 구분하고 404/정상 빈 결과는 `no_records`로 보존한다. PubChem의
`Activity Value [uM]`는 Kd/Ki/IC50 이름이 함께 있을 때만 정량화한다.

### Assay SQL 저장

- `dta_assay_queries`: tool/request/analysis/run ID, provider와 고정 version, SMILES hash,
  UniProt accession, 성공·무결과·실패 상태, 외부 요청 수와 실행 시간을 저장한다.
- `dta_assay_evidence`: 조회별 순서와 원 레코드 ID, 정량 endpoint/value/unit 또는 정성 outcome,
  assay 설명과 DOI/PMID를 저장한다.

분석 원문 SMILES는 `analyses.canonical_smiles`가 소유하므로 조회 테이블에는 SHA-256만 남긴다.
단백질 서열과 provider 원본 payload는 저장하지 않는다. 성공·`no_records`·실패를 모두 완료된
outcome으로 보존하며 동일 tool call 재전달은 SQL 결과를 복원해 외부 API를 다시 호출하지 않는다.
새 외부 조회가 필요하면 새 request/tool call ID를 사용한다. `agent_runs.output_json`은 UI와 Agent에
전달된 versioned snapshot이며 이 정규화 원장을 대체하지 않는다.
새 DTA snapshot은 후보별 `assay_runs`에 provider source, tool call ID, outcome과 외부 요청 수를
함께 보존한다. Decision은 이를 SQL 원장과 대조한 뒤 제한된 핵심 근거만 입력으로 투영한다.
필드가 없는 #258 이전 snapshot은 기본 빈 목록으로 읽는다.

전용 DB session으로 저장하며 성공 시 commit한다. 동일 ID·동일 결과 재전달만 멱등 처리하고,
재추론은 새 request/tool call ID를 사용한다. 결과 소요 시간이 달라져도 같은 ID로 덮어쓰지 않는다.
현재 분석 입력에 canonical target sequence가 없으므로 저장 시 분석의 SMILES만 교차 검증한다.
target sequence와 hypothesis의 연결은 후속 orchestration에서 검증해야 한다.

## 실패와 실행 경계

timeout과 명시적 provider 장애만 `unavailable`로 반환한다. 관측값을 0으로 채우지 않는다.
DeepPurpose는 별도 Python 환경에서 실행하며 API에 torch를 설치하지 않는다.
Celery task·전체 분석 orchestration에는 아직 연결하지 않는다.
provider는 취소 가능한 비동기 I/O를 구현해야 한다. 같은 이벤트 루프에서 CPU 추론을 실행하면
async timeout이 중단시킬 수 없다. timeout은 원격 작업의 강제 종료도 보장하지 않는다.

OOM으로 API 프로세스가 죽으면 이 adapter의 예외 처리나 DB 저장은 실행되지 않는다.
도메인 테이블은 완료 결과 저장소이며, 공통 `tool_executions`가 실행 전 running을 기록한다.
worker 종료 후 만료된 lease는 별도 회수 실행으로 실패 처리한다. 자동 재시도와 서로 다른
호출 사이의 전역 동시 실행 제한은 후속 orchestration의 책임이다.
exit code 137만으로 OOM을 단정하지 않고 Render event와 memory limit 로그를 함께 확인한다.

## 검증과 확장

backend에서 `uv run --no-env-file pytest tests/test_dta.py`를 실행한다.
테스트는 합성 provider와 SQLite를 쓰며 Docker·외부 API·모델 추론을 실행하지 않는다.
`alembic upgrade head`로 migration을 적용한다. 실제 배포 DB 적용은 별도 절차다.

DeepPurpose provider의 command는 서버 운영자가 고정한 argv다. LLM·HTTP 입력에서 받지 않는다.
예: `DeepPurposeProvider(("/opt/dta/.venv/bin/python", "/opt/dta/runtime.py", "--model", "MPNN_CNN_BindingDB"), MPNN_CNN_BINDINGDB_MODEL)`.
이 경로는 배포 예시이며 현재 Render에 설치된 경로가 아니다. 해당 실행 환경에 검증된
checkpoint와 smoke와 동일한 의존성이 준비되어야 한다. shell wrapper 없이 Python을 직접 실행한다.
프로세스는 첫 호출에 생성하고 모델을 재사용한다. worker/event loop별 provider 하나를 재사용하고
종료 시 `await provider.aclose()`를 호출한다. 여러 worker이면 모델 메모리도 각각 필요하다.

`DtaToolAdapter`로 timeout을 설정한다. 직접 `predict`를 호출하면 자체 timeout은 없으며,
`aclose`는 현재 요청 종료를 기다린다. timeout·호출 취소 시 자식 프로세스를 종료하고 다음 요청에
새로 생성한다. 대기 중인 호출만 취소되면 현재 추론은 유지한다. startup 취소 시에도 PID를 회수한다.
CPU thread는 1개, DataLoader worker는 0개다. checkpoint 파일 hash를 unpickle 전에 검사한다.

응답의 request ID·입력 hash·model identity·score type을 검증한다. 모델 stderr는 원문 노출 및
pipe 정체를 피하기 위해 버린다. import/load·추론 시간과 프로세스 누적 peak RSS는 로그의
`dta_runtime` 필드에 남기며 integration report에서도 확인한다. 기본 문자열 formatter에서는
extra field가 보이지 않을 수 있다. SQL에는 기존 총 duration만 저장한다.

SQL 서비스는 완료된 동일 호출을 재실행하지 않는다. unavailable도 완료된 관측으로 유지하므로
재시도는 새 request/tool call ID를 사용한다. 동시에 도착한 같은 ID의 호출은 차단한다.
작업 시작 commit과 lease는 [공통 실행 계층](../tool_execution/README.md)을 사용한다.
서버 프로세스의 재시작 자체는 호스팅 플랫폼의 책임이다.
MPNN-CNN의 실제 linux/amd64 CPU peak RSS·cold/warm latency·재현성은 아직 실모델
smoke로 검증하지 않았으며, 운영 배포 전 `run-provider.sh` 결과를 별도로 확정해야 한다.
MAMMAL/Boltz는 후보로만 유지한다.
새 score 유형은 계약·단위 검증·SQL CHECK와 migration을 함께 수정한다.
배포 조건은 [DTA 실행과 Render 자원 정책](../../../../docs/features/dta-provider.md)을 따른다.
실제 Docker 통합 검증은 [runtime 실행 안내](../../../../experiments/deeppurpose-dta-smoke/README.md)를 따른다.
