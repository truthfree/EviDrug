# Target Prioritization v2

## 배경과 사용자 목표

질환 association이 높은 유전자가 곧 소분자 약물 표적이라는 보장은 없다. 사용자는 질환
관련성, modality별 tractability와 기전 적합성이 구분된 shortlist를 확인하고, 근거 부족을
부적격으로 오해하지 않은 상태에서 후속 DTA 결과를 비교할 수 있어야 한다.

## 포함 및 제외 범위

포함 범위는 Open Targets small-molecule tractability, 결정적 자격 정책, evidence-only LLM
shortlist, 후보별 UniProt sequence 검증, 공개 API 요약과 다중 DTA 실행 계약 설계다.

실제 DTA Agent와 fan-out 실행, ADMET·Decision Agent, 프론트 결과 UI, 유전자 변이별 기전
판정과 임상 치료 추천은 제외한다. 현재 runtime은 Target 완료 후 미구성 DTA Agent를 한 번
시작하므로 분석 전체는 계속 실패할 수 있다.

## 정상 흐름

1. 질환과 optional specified target을 입력한다.
2. 서버가 direct association과 small-molecule tractability를 같은 Open Targets release에서
   조회한다.
3. reviewed protein이 없는 후보는 명시적인 사유로 제외한다.
4. 임상 선례, ligand, pocket 또는 druggable-family 근거가 있으면 `eligible`, 양성 근거가
   없으면 `exploratory`로 분류한다.
5. 모델이 제공된 후보 안에서만 순위, 조절 방향과 설명을 반환한다.
6. shortlist sequence를 각각 검증하고 실패 후보만 격리한다.
7. sequence를 제외한 결과 요약을 analysis polling API로 제공한다.

## 입력과 출력

현재 SMILES 입력은 therapeutic modality를 `small_molecule`로 고정한다. Target 모델에는
SMILES나 protein sequence를 전달하지 않는다.

출력은 primary, alternatives와 excluded candidates로 구성한다. 추천 후보에는 association,
원 tractability assessments, 자격과 사유 코드, 조절 방향, rationale, 검증된 UniProt sequence와
hash가 들어간다. API summary에서는 큰 sequence를 제외한다.

`exploratory`는 양성 tractability 근거가 확인되지 않았다는 뜻이다. `ineligible`은 향후
지원 modality와 명백히 호환되지 않는 결정적 조건에만 사용하며 이번 정책은 질환 종류나
근거 부재만으로 이를 만들지 않는다.

## 다중 DTA fan-out/fan-in 계약

후속 DTA Agent는 한 `agent_run` 안에서 Target shortlist를 읽고 후보별로 독립적인 `dta`
tool call을 생성한다.

- 최대 후보 수는 저장된 shortlist와 실행 profile 상한 중 작은 값으로 고정한다.
- 후보마다 독립 `tool_call_id`, input fingerprint, timeout, 상태와 원 점수를 저장한다.
- 한 후보의 timeout/unavailable은 다른 후보 결과를 취소하거나 score 0으로 바꾸지 않는다.
- 일부 성공이면 DTA Agent는 `partial_failure`, 전부 실패하면 `failed`를 반환한다.
- fan-in은 서로 다른 score type이나 단위를 평균하지 않고 후보별 observation을 그대로 보존한다.
- Decision에는 성공·실패 후보, 모델/version, 비용과 latency를 모두 전달한다.
- CPU/GPU provider의 실제 병렬 실행은 resource semaphore와 측정 결과가 마련되기 전 허용하지
  않는다. 처음에는 결정적인 순차 실행을 기본으로 한다.

현재 `provides_dta_input=True`는 하나 이상의 검증된 shortlist 후보가 있다는 뜻이다. 실제
fan-out은 백엔드 Issue #96에서
구현하고 기존 `DtaExecutionService`와 공통 execution ledger를 재사용한다.

## API와 화면 상태

`GET /api/v1/analyses/{analysis_id}`는 optional `target_prioritization`을 반환한다.

- `eligible`: 소분자 양성 근거가 있음
- `exploratory`: 양성 근거가 확인되지 않아 추가 검증 필요
- 제외 사유: reviewed sequence 부재, shortlist 범위 밖, sequence 검증 실패
- `null`: Target 미완료, 실패 또는 과거 v1 결과

프론트는 이 값을 성공/실패 색상으로 단순화하지 않고 근거 있음, 추가 검증 필요, 제외를
구분해야 한다. 실제 UI 구현은 프론트
Issue #97과 별도 PR로 진행한다.

## 보안, 비용과 재현성

- 키, 전체 prompt, SMILES와 protein sequence를 로그에 남기지 않는다.
- 후보 수와 shortlist 상한으로 Dacon token과 향후 DTA 비용을 제한한다.
- source API/data release, 조회 시각, prompt·policy version과 실제 사용량을 저장한다.
- live provider는 snapshot 재현을 보장하지 않으며 평가 dataset에는 별도 versioned fixture가
  필요하다.

## 완료 조건

- association과 tractability가 서로 다른 필드와 근거 claim으로 저장된다.
- 근거 부족은 `exploratory`, 결정적 제외는 reason code로 구분된다.
- 모델의 ID와 후보 수가 서버 allowlist 검증을 통과한다.
- discover가 검증된 ranked shortlist를 반환한다.
- Palbociclib specified CDK4 case가 `eligible` primary로 유지된다.
- API가 sequence 없는 결과 요약을 제공한다.
- 다중 DTA 비용·실패 격리·fan-in 계약이 문서화된다.

관련 백엔드 Issue #95.
개별 인과 근거와 치료 방향 계약은 후속
[Target causal support](./target-causal-support.md)에서 확장한다.
