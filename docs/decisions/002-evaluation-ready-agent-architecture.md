# 002. 평가 가능한 에이전트 아키텍처

## 상태

- 상태: 채택
- 결정일: 2026-09-17
- 관련 Issue: [#34](https://github.com/truthfree/Dacon2026/issues/34)

## 상황

EviDrug는 Target Hypothesis, DTA, ADMET 및 Decision Agent를 조합해 초기 후보물질의
판단을 지원한다. 최종 판정만 저장하면 어떤 근거와 경로가 판단을 만들었는지 복원할 수
없고, Vanilla LLM이나 단순 workflow와 공정하게 비교할 수도 없다. 반대로 평가 전용
분기를 production Agent 안에 넣으면 실제 서비스와 평가 대상의 동작이 달라진다.

Agent 구현 전에 다음 경계를 고정해야 한다.

- production과 evaluation이 같은 Agent 구현을 호출하는 경계
- Agent 입력·출력, 근거와 실행 추적의 공통 형태
- orchestration이 담당할 routing, 재호출과 실패 전파
- 모델, 프롬프트, 도구와 데이터 버전을 재현하는 방법
- baseline마다 달라질 수 있는 기능과 공통으로 유지할 조건

## 결정

### 1. Agent와 orchestration을 분리한다

각 Agent는 하나의 명시적인 입력을 받아 구조화된 출력을 반환한다. Agent는 다른 Agent를
직접 호출하거나 전체 분석 상태를 변경하지 않는다. orchestration이 다음 책임을 가진다.

- 실행 가능한 단계 판단
- Agent 호출과 결과 저장
- 단계 상태 전이와 실패 전파
- Decision이 제안한 후속 요청의 정책 검증과 제한된 실행
- 동일 요청 반복과 순환 차단
- 최종 분석 상태 결정

API와 Celery task는 orchestration의 진입점일 뿐 Agent의 도메인 판단을 구현하지 않는다.

#### Decision Agent와 orchestration의 경계

Decision Agent는 **무엇을 판단하며 어떤 추가 근거가 필요한지**를 제안하고,
orchestration은 **무엇을 지금 실행할 수 있는지**를 결정한다. Decision Agent가 Agent나 도구를
직접 호출하지 않으며 orchestration이 Go/Conditional Go/No-Go 판정이나 과학적 근거를
수정하지 않는다.

| 책임 | Decision Agent | Orchestration |
| :--- | :--- | :--- |
| 입력 근거 해석 | 근거의 지지·위험·상충·공백을 해석 | schema와 참조 무결성만 검증 |
| 후보 판정 | Go, Conditional Go, No-Go와 판단 이유 제시 | 판정을 저장·전달하되 변경하지 않음 |
| 후속 정보 | 필요한 정보, 이유와 판단에 미칠 영향을 선언적으로 요청 | 허용 목록, 의존성, 예산과 중복 여부를 검사해 승인 또는 거부 |
| 실행 순서 | 직접 결정하거나 호출하지 않음 | DAG와 상태를 기준으로 호출·병렬 실행·skip 결정 |
| 재시도 | 같은 호출의 기술적 재시도를 명령하지 않음 | timeout 등 일시 오류에 고정 정책으로 기술적 재시도 |
| 재분석 | 과학적 불확실성을 줄일 의미 있는 후속 분석을 제안 | 입력·허용 도구·중복·recall 상한을 확인한 뒤 새 run으로 실행 |
| 상태 | 판정의 `provisional` 또는 `final` 단계만 반환 | `queued`, `running`, `completed`, `partial_failure`, `failed` 관리 |
| 실패 처리 | 이용 가능한 근거로 판단하고 남은 공백을 설명 | 실패 분류, 단계 전파, retry/skip과 전체 종료 상태 결정 |

Decision Agent 입력은 완료된 Agent 출력과 명시적인 누락·실패 요약으로 만든 immutable
snapshot이다. queue 상태, Celery 예외, retry 횟수와 비밀 설정은 전달하지 않는다. 기술적
실패가 재시도 정책을 모두 소진한 뒤에는 orchestration이 `unavailable` 결과로 변환하며,
Decision Agent는 이를 과학적 부정 근거가 아니라 데이터 공백으로 다룬다.

Decision Agent의 후속 요청은 임의의 tool call이 아니라 다음처럼 제한된 데이터다.

```text
FollowupRequest
├── request_id
├── target_agent
├── objective
├── required_fields
├── reason_claim_ids
└── expected_decision_impact
```

orchestration은 요청을 다음 순서로 처리한다.

1. 출력 schema와 `claim_id` 참조를 검증한다.
2. `target_agent`와 `required_fields`가 허용 목록에 있는지 확인한다.
3. 선행 입력, 전체 시간·호출·token 예산과 Agent별 recall 상한을 확인한다.
4. 입력, 모델·도구·데이터 버전과 요청 조건의 hash로 동일 실행의 반복을 차단한다.
5. 승인 시 새 `run_id`로 실행하고, 거부 시 고정된 reason code를 기록한다.
6. 새 결과 또는 거부 사유가 포함된 snapshot으로 Decision Agent가 최종 판단하게 한다.

대표적인 경계 사례는 다음과 같다.

- ADMET 제공자 timeout은 orchestration이 기술적으로 재시도한다. 재시도 소진 후 Decision
  Agent는 `ADMET unavailable`을 받아 공백과 불확실성을 판단한다.
- DTA와 ADMET 근거가 상충하면 Decision Agent가 필요한 추가 항목을 요청할 수 있다.
  orchestration은 요청을 실행할 수 있는지 검사하지만 상충의 의미를 다시 해석하지 않는다.
- 표적 서열이 없으면 orchestration이 DTA를 `skipped_due_to_missing_target`으로 만든다.
  Decision Agent는 DTA의 부정적 결과가 아니라 결합 근거의 부재로 해석한다.
- Decision 출력 schema가 잘못되면 orchestration이 제한된 형식 수정 호출을 수행한다. 내용을
  대신 작성하지 않으며 수정 실패 시 Decision 단계를 실패로 기록한다.

따라서 **domain verdict의 소유자는 Decision Agent**, **workflow state의 소유자는
orchestration**이다. Decision의 후속 요청은 명령이 아니라 제안이며, orchestration의 거부는
과학적 반박이 아니라 실행 정책의 결과다.

세 상태 공간은 필드와 열거형을 공유하지 않는다.

- `analysis_status`: orchestration이 관리하는 작업 수명주기
- `decision_phase`: Decision 출력이 잠정 판단인지 최종 판단인지 표시
- `verdict`: 후보의 Go, Conditional Go, No-Go 도메인 판정

예를 들어 ADMET 실패 후 남은 근거로 최종 판단을 만들면 `decision_phase=final`,
`verdict=conditional_go`, `analysis_status=partial_failure`가 동시에 가능하다. `final`은 모든
계산이 성공했다는 뜻이 아니며 `completed`도 자동으로 Go를 의미하지 않는다.

#### 전문 Agent의 자율적인 조사와 도구 선택

DTA와 ADMET Agent는 기본 예측 도구 실행뿐 아니라 자신의 분야에서 결과의 충분성,
적용 범위와 상충을 해석하고 추가 조사 방법을 선택한다. 최종 후보 판정은 Decision이
소유하며 전문 Agent는 분야별 결론, 근거와 한계를 반환한다.

| 판단 대상 | 책임자 |
| :--- | :--- |
| 어떤 공백이 후보의 종합 판정에 중요한가 | Decision |
| 어떤 모델·자료·조회가 해당 분야의 공백을 해소할 수 있는가 | 전문 Agent |
| 요청된 도구와 입력이 허용되고 실행 예산이 남았는가 | Orchestration의 공통 실행 계층 |

Decision은 “모델 B 실행”보다 “간독성 근거의 신뢰도를 재검토”처럼 조사 목적을 요청한다.
`required_fields`는 필요한 근거 항목이며 특정 모델 사용을 강제하지 않는다. 전문 Agent는
기본 결과를 검토하다 발견한 공백에 대해서도 배정된 범위와 예산 안에서 스스로 추가 조사를
시작할 수 있다. 이러한 내부 도구 호출마다 Decision의 승인을 받을 필요는 없다.

전문 Agent의 한 run은 다음 순환을 포함할 수 있다.

1. 기본 도구 결과와 현재 조사 목적을 확인한다.
2. 부족한 근거와 추가 조사로 기대하는 정보를 명시한다.
3. 등록된 도구 중 적절한 것을 선택해 구조화된 `ToolRequest`를 반환한다.
4. 공통 실행 계층이 입력·권한·예산·중복을 검증하고 도구를 실행한다.
5. 전문 Agent가 반환된 관측을 해석하고 다음 도구 또는 종료를 선택한다.
6. 근거, 분야별 결론, 상충과 해결되지 않은 공백을 반환한다.

`ToolRequest`는 `request_id`, `run_id`, `tool_id`, `tool_version`, 타입이 지정된
`arguments`, `objective`, 관련 `claim_id` 또는 `gap_id`를 가진다. 실행 계층은 요청별
승인·거부, 관측 결과 참조와 사용량을 기록한다. 전문 Agent의 내부 도구 호출은 `tool_call_id`,
Decision 요청에 의한 재분석은 새 `run_id`로 구분하고 둘 다 전체 분석 예산을 소비한다.
Agent는 임의 도구를 설치하거나 자신의 실행 한도를 늘릴 수 없다.

Orchestration은 추가 조사의 과학적 유용성을 판단하지 않는다. 이 판단은 전문 Agent와
Decision의 책임이며, 실행 계층은 기계적으로 검증할 수 있는 조건만 적용한다. 기술적 retry,
전문 Agent 내부의 도구 선택, Decision이 요청한 recall을 별도로 추적한다.

도구 등록 정보에는 예측 endpoint, 입력 요건, 출력 단위, 적용 범위, 알려진 한계,
학습 데이터 출처, 모델·데이터 버전과 자원 요구량을 포함한다. 알려지지 않은 항목은
`unknown`으로 보존한다. 추가 모델의 출력이 기존 출력과 같은 endpoint인지, 학습 데이터가
중복되는지 확인하며 모델 수나 다수결을 근거의 독립성·신뢰도로 간주하지 않는다.

추가 결과가 상충하면 이를 보존한다. 원하는 판정이 나올 때까지 도구를 바꾸지 않으며,
적합한 도구가 없거나 더 조사할 가치가 없다고 판단하면 공백과 후속 실험 제안으로 종료한다.
예산 소진 시 실행 계층은 더 이상의 도구 호출을 차단한다. 최종 정리를 위한 예산을 미리
예약하고, 정리 호출도 실패하면 기존 관측을 보존한 실패 상태를 반환한다.

#### 원본 결과와 판단 컨텍스트

원본 예측·조회 결과는 저장소에 보존하고, LLM에는 현재 질문에 필요한 구조화된 관측을
전달한다. 수치 계산, 단위 검증과 기계적인 데이터 전달은 코드로 수행한다. 전문 Agent의
계획·해석과 Decision의 종합 판단에서 발생한 토큰은 각각 기록한다.

Decision에 전달하는 요약에는 출처 참조, 지지·위험 근거, 상충, 적용 범위와 결측 사유를
유지한다. 요약에 사용한 원본 결과와 변환 버전을 추적하고 필요한 상세 근거를 다시 요청할
수 있게 한다. 컨텍스트 절감 때문에 불리한 근거나 모델 간 불일치가 누락되어서는 안 된다.

### 2. 공통 실행 계약을 사용한다

모든 production Agent와 baseline adapter는 논리적으로 동일한 `AgentInput`과
`AgentOutput` 계약을 사용한다. Agent별 전용 payload는 명시적인 schema로 분리하되 다음
공통 필드를 유지한다.

```text
AgentInput
├── analysis_id
├── run_id
├── agent_name
├── attempt
├── case_input
├── upstream_outputs
└── execution_limits

AgentOutput
├── status
├── result
├── evidence_claims
├── gaps
├── warnings
├── requested_followups
└── execution_metadata
```

`case_input`은 사용자가 확정한 질환, 선택적 타깃과 canonical SMILES를 보존한다.
`upstream_outputs`에는 현재 Agent가 실제로 의존하는 결과만 전달한다. 임의의 `dict` 대신
Pydantic model과 열거형을 사용하고 schema version을 기록한다.

Decision 전용 출력에는 `decision_phase`, `verdict`, `rationale`, `used_claim_ids`,
`conflicts`, `gaps`와 `followup_requests`를 둔다. `decision_phase=provisional`은 허용된 후속
요청의 실행 여부가 아직 확정되지 않았음을 뜻하고, `final`은 현재 확보 가능한 근거로 더 이상
workflow 호출을 요구하지 않는다는 뜻이다. `final`도 불확실성과 미해결 공백을 포함할 수 있다.

### 3. 주장과 출처를 구조화한다

자유 텍스트 설명만 저장하지 않고 모든 핵심 판단 근거를 `EvidenceClaim`으로 표현한다.

| 필드 | 의미 |
| :--- | :--- |
| `claim_id` | 한 분석 안에서 안정적인 주장 식별자 |
| `claim_type` | target association, binding, ADMET risk 등 주장 유형 |
| `statement` | 사용자가 읽을 수 있는 짧은 주장 |
| `direction` | 판단을 지지, 반대 또는 불확실하게 만드는 방향 |
| `value` / `unit` | 수치 근거가 있을 때의 값과 단위 |
| `confidence` | 제공자가 산출한 신뢰 정보와 산출 방식 |
| `source` | 데이터베이스, 논문, 모델 또는 도구 식별자 |
| `source_record_id` | 원본 레코드나 accession |
| `retrieved_at` | 외부 근거를 조회한 시각 |
| `producer_run_id` | 주장을 생성한 Agent 실행 |

`confidence`를 서로 다른 제공자 사이의 공통 확률처럼 해석하지 않는다. 모델 점수,
percentile, 실험값과 LLM 판단을 원래 의미가 유지되는 형태로 기록한다.

### 4. 실행 추적은 append-only record로 남긴다

상태를 덮어쓰는 현재값과 별도로 `ExecutionTrace` event를 추가만 가능한 기록으로 남긴다.
각 event는 다음 정보를 포함한다.

- `analysis_id`, `run_id`, `parent_run_id`
- Agent 또는 orchestration 단계
- queued, started, completed, failed, skipped, recalled 상태
- routing 선택과 선택 이유
- 재호출 대상, 사유와 이전 결과 참조
- 입력·출력 schema version과 content hash
- 모델 이름과 버전, prompt template ID와 version
- 도구, 패키지, 모델 가중치와 데이터 snapshot version
- 시작·종료 시각, latency, token 및 도구 호출 수
- 오류 분류와 재시도 가능 여부

Decision의 후속 요청과 orchestration의 승인·거부는 서로 다른 event로 기록한다. 승인 여부와
고정 reason code, 적용한 orchestration policy version을 남겨 Decision의 요청 품질과 실행
정책 준수 여부를 별도로 평가할 수 있게 한다.

접근 코드, 세션 토큰, API key와 전체 민감 입력은 trace에 넣지 않는다. 원본 SMILES와 분석
결과는 접근 제어된 분석 레코드에 저장하고 일반 로그에는 식별자와 hash만 남긴다.

### 5. production과 evaluation은 runner에서만 갈라진다

Agent 구현은 실행 환경을 알지 않는다. production runner는 데이터베이스와 Celery를 통해
Agent를 호출하고, evaluation runner는 동일한 adapter를 고정된 `EvaluationCase`에 적용한다.

```text
EvaluationCase
├── case_id
├── input
├── expected_decision
├── expected_reasons
├── gold_identifiers
├── allowed_tools
└── resource_limits
```

Always-Go, Vanilla LLM, Single LLM + Tools, Staged LLM, Full EviDrug은 runner가 선택하는
서로 다른 strategy다. 최종 출력 schema와 case는 동일하게 유지한다. 평가용 정답이나
조건문을 production Agent와 prompt에 주입하지 않는다.

#### Ablation을 위한 실행 설정과 교체 지점

후속 구현은 동일한 Agent와 도구 adapter를 유지한 채 실행 설정으로 구성요소를 바꿀 수 있게
한다. 실행 시작 시 versioned `ExecutionProfile`을 검증하고 실제 적용된 설정을 고정한다.
production도 같은 설정 계약을 사용한다. `is_evaluation` 같은 조건을 Agent 내부에 흩뿌리지
않고 runner가 도구 목록과 정책 구현을 주입한다.

최소 교체 지점은 다음과 같다.

- `ToolRegistry`: Agent별 허용 도구와 고정 버전
- `ToolSelectionPolicy`: 고정 계획 또는 Agent가 선택하는 조사 계획
- `RecallPolicy`: Decision의 후속 요청 실행 여부와 상한
- `ContextBuilder`: 원본 결과를 판단 입력으로 변환하는 방식과 버전
- `ToolExecutor`: 실제 실행 또는 기록된 관측 재생

전문 Agent별로 도구 집합과 선택 정책을 독립 설정한다. 예를 들어 ADMET만 자율 선택으로
바꾸고 DTA의 계획과 도구는 유지할 수 있어야 한다. Decision recall을 끄더라도 전문 Agent
내부의 자율 도구 선택은 유지할 수 있고, 반대 조합도 지원한다. 고정 계획 모드에서는
도구 선택 LLM을 호출하지 않고 동일한 관측 형식으로 결과를 전달한다.

실행 계층이 허용 도구와 호출 차단을 강제하며 prompt 지시만으로 기능을 비활성화하지 않는다.
비활성 구성요소는 `disabled_by_profile`로 기록하여 실행 실패나 입력 부족과 구분한다.
사용할 수 없는 결과를 빈 정상 결과로 대체하지 않는다. 지원하지 않는 설정 조합은 실행 전에
거부하고 다른 정책으로 조용히 바꾸지 않는다.

profile 이름뿐 아니라 펼쳐진 실제 설정과 hash, code commit, prompt·모델·도구·데이터·정책
버전과 실제 사용량을 보존한다. 기록된 관측 재생은 Decision이나 routing의 영향만 분리해
검사할 때 사용하고, 실도구 실행의 비용·latency 평가와 구분한다.

### 6. 자동 평가와 인간 평가를 분리한다

다음 항목은 결정론적으로 계산 가능한 자동 평가로 처리한다.

- disease top-k 포함률과 top-1 정확도
- 사용자 확정 뒤 disease ID 보존
- UniProt canonical accession 정확도와 target substitution error
- DTA/ADMET benchmark 성능
- Go/Conditional Go/No-Go confusion matrix, balanced accuracy와 macro-F1
- routing, recall, loop, completion 및 schema compliance
- latency, token, 도구 호출과 추정 비용

근거의 충분성, 위험 설명과 후속 실험의 유용성처럼 의미 판단이 필요한 항목은 blind human
review로 분리한다. 평가자는 0/0.5/1 rubric을 사용하고 두 명이면 Cohen's kappa, 세 명
이상이면 Fleiss' kappa를 보고한다.

### 7. 실패와 부분 결과도 평가 데이터다

외부 제공자 장애, schema 오류, 입력 부족과 적용 범위 이탈을 하나의 실패로 합치지 않는다.
이미 확보한 근거는 보존하며 DTA의 선행 표적 누락은
`skipped_due_to_missing_target`으로 기록한다. 실행 완료율을 높이기 위해 오류를 빈 정상값으로
바꾸지 않는다.

## 검토한 대안

### 최종 결과만 저장

구현은 단순하지만 근거 연결, routing 정확도, 재호출 필요성과 비용을 사후에 평가할 수 없다.

### 평가 전용 Agent 복사본

평가 코드를 빠르게 만들 수 있지만 production과 동작이 달라져 결과의 대표성이 사라지고
두 구현이 쉽게 어긋난다.

### 하나의 범용 JSON payload

초기 변경은 쉽지만 필수 필드와 상태 규칙을 검증하기 어렵고 Agent 사이의 암묵적 결합이
커진다. 공통 envelope와 Agent별 versioned schema를 함께 쓰는 방식을 선택한다.

## 감수하는 단점

- 실행 추적과 근거 레코드 때문에 저장 구조와 테스트가 증가한다.
- schema version과 데이터 snapshot을 관리해야 한다.
- 외부 데이터베이스가 과거 snapshot을 제공하지 않으면 완전한 재현이 어려울 수 있다.
- human evaluation에는 도메인 평가자 시간과 일치도 관리가 필요하다.

이 비용은 평가를 나중에 복원할 수 없게 되는 위험보다 작다고 판단한다.

## 재검토 조건

- trace 저장 비용이 실제 분석 비용의 의미 있는 비중을 차지할 때
- 외부 모델 서비스가 필요한 버전·사용량 정보를 제공하지 않을 때
- 평가 runner가 production adapter를 재사용할 수 없는 실행 제약이 확인될 때
- 후보물질 또는 조직별 접근 제어가 추가되어 trace의 보안 경계가 바뀔 때

## 구현 영향

- 분석 작업과 Agent 실행 테이블은 현재 상태와 append-only event를 분리한다.
- 새 Agent는 고정 입력·출력 계약, 실패 방식과 비교 가능한 검증 기준을 함께 정의한다.
- Decision 출력은 사용한 `claim_id`를 참조해야 하며 출처 없는 핵심 주장을 허용하지 않는다.
- 평가 runner와 dataset 구축은 Agent 구현과 분리된 후속 Issue로 진행한다.
