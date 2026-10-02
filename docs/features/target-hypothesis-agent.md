# Target Hypothesis Agent와 DTA 입력 연결

## 배경과 목표

고정 DAG는 Target Hypothesis 성공 여부에 따라 DTA를 시작하지만 기존 worker에는 실제 Agent가
없어 항상 `agent_not_configured`로 끝났다. 첫 production Agent로 질환-표적 근거를 조회하고,
출처가 검증된 단백질 서열만 DTA 입력으로 전달한다.

## 포함 및 제외 범위

포함 범위는 Open Targets direct association 후보 조회, Dacon 모델의 제한된 후보 선택,
UniProtKB/Swiss-Prot sequence 확인, typed `AgentOutput`, worker runtime 연결과 영속 trace다.

ADMET·DTA·Decision Agent, 자동 재시도, 결과 화면, Open Targets·UniProt snapshot과 평가 runner는
제외한다. 따라서 Target이 성공해도 나머지 Agent가 미구성이면 전체 분석은 아직 실패할 수 있다.

## 정상 흐름

1. orchestration이 Target 단계의 공통 `AgentInput`을 전달한다.
2. `specified`는 사용자 표적과 정확히 일치하는 후보를, `discover`는 질환 상위 후보를 조회한다.
3. reviewed Swiss-Prot accession이 있는 후보만 모델에 제공한다.
4. 모델은 제공된 Ensembl ID 중 하나와 선택 설명만 반환한다.
5. 서버가 allowlist를 재검증하고 UniProt에서 human reviewed sequence를 조회한다.
6. 표준 아미노산 sequence와 SHA-256, 근거, 경고와 사용량을 `agent_runs`에 저장한다.
7. 모든 검증이 통과하면 DTA가 시작되고, 아니면 DTA는 missing target으로 건너뛴다.

## 입력과 출력

입력은 공통 `AgentInput`의 disease ID/name, `discover|specified`, optional target name이다.
SMILES와 UniProt sequence는 모델 후보 선택 prompt에 포함하지 않는다.

출력은 Target mode, Ensembl ID, approved symbol/name, Swiss-Prot accession, protein name,
sequence와 hash, association/data type scores, 모델 선택 설명을 포함한다. 근거 claim은
Open Targets disease-target record와 UniProt accession을 각각 가리킨다.

## 보안, 비용과 과학적 제약

- Dacon 키와 전체 요청·sequence를 로그에 기록하지 않는다.
- 모델은 서버가 제공한 ID allowlist 밖을 선택할 수 없다.
- 모델이 protein sequence 또는 provider score를 생성하지 않는다.
- association score는 후보 순위 휴리스틱이며 confidence probability가 아니다.
- 외부 호출은 정상 경로에서 Open Targets, Dacon, UniProt 각 1회다.
- `specified` alias resolution은 Open Targets search 한 번을 추가한다.
- provider는 fixed adapter dependency이며 LLM-visible tool이 아니다. adaptive tool로 공개할
  때는 ToolRegistry, admission, 실행 ledger와 예산을 별도 Issue에서 연결한다.
- Target runtime은 Dacon SDK 자동 재시도를 끄고 실패를 typed output으로 보존한다.
- live source의 조회 시각은 남기지만 release snapshot 재현성은 후속 범위다.

## 오류와 상태

후보 없음과 검증 실패는 재시도하지 않는 typed failure다. 외부 provider 또는 모델 장애는
retryable flag를 보존하지만 이번 범위에서 자동 재시도하지 않는다. 실패 output은
`agent_runs.output_json`, 오류 코드는 `agent_runs.error_code`, 상태 변화는
`execution_trace_events`에 남는다.

## 완료 조건

- 두 target mode가 동일한 typed 결과 계약을 사용한다.
- 모델 출력과 UniProt sequence를 서버가 독립 검증한다.
- 성공 결과만 DTA 입력을 활성화한다.
- fake provider 기반 단위·orchestration 통합 테스트가 통과한다.
- Docker Compose 실제 실행에서 Target 완료 상태와 DB output을 확인한다.

관련 백엔드 [Issue #93](https://github.com/truthfree/EviDrug-Dacon2026/issues/93).

Issue #93의 단일 association 후보 선택 계약은 후속
[Target Prioritization v2](./target-prioritization-v2.md)에서 tractability 기반 shortlist로
확장됐다. 이 문서는 최초 production Agent 연결 범위를 기록하며 현재 동작은 v2 명세를 따른다.
