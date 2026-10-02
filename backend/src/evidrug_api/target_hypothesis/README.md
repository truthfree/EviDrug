# Target Prioritization Agent

## 목적과 책임

질환 연관성, 개별 인과 근거와 소분자 tractability를 별개로 보존하고, Dacon 모델이 조회된 후보와
근거 안에서만 DTA shortlist를 정렬하게 한다. 후보별 UniProtKB/Swiss-Prot reviewed human
sequence가 검증된 경우에만 DTA 입력으로 내보낸다.

유전질환이나 높은 genetic association을 일괄 제외하지 않는다. 양성 tractability 근거가
없으면 `ineligible`로 단정하지 않고 `exploratory`와 evidence gap으로 보존한다. 모델이
association score, tractability, 단백질 서열 또는 임상 근거를 생성하지 못하도록 식별자와
개수 allowlist를 서버에서 재검증한다.

## 폴더 구조와 읽는 순서

```text
agent.py      조회, 정책, shortlist, sequence 검증과 AgentOutput 조립
providers.py  Open Targets association/tractability/개별 evidence, Pharos 성숙도와 UniProt 조회
causal_policy.py  인과 상태와 evidence 기반 치료 방향의 비-LLM 정책
policy.py     small-molecule 자격을 결정하는 비-LLM 정책
reasoner.py   allowlist 안의 순위와 기전 해석을 만드는 Dacon 경계
contracts.py  외부 근거, shortlist와 검증된 DTA 입력 계약
runtime.py    worker 설정으로 실제 client 수명주기 구성
```

`agent.py → providers.py → causal_policy.py → policy.py → reasoner.py → contracts.py → runtime.py`
순서로 읽는다.

| 이름 | 위치 | 역할 | 입력·출력 | 주요 오류 |
| :--- | :--- | :--- | :--- | :--- |
| `TargetHypothesisAgent.execute` | `agent.py` | 전체 Target 수직 흐름 | `AgentInput` → shortlist `AgentOutput` | 안정적인 typed failure |
| `OpenTargetsCandidateProvider.find_candidates` | `providers.py` | direct association과 target annotation 조회 | disease와 optional target → 후보·제외 목록 | unavailable, not found |
| `PharosTargetProvider.get_target_evidence` | `providers.py` | 표적 성숙도와 지식 공백 조회 | UniProt accession → TDL·family·novelty·ligand/publication count | unavailable, not found |
| `CausalSupportPolicy` | `causal_policy.py` | 생물학적 인과 상태와 치료 방향 정규화 | 개별 evidence → causal support | 없음 |
| `SmallMoleculePrioritizationPolicy` | `policy.py` | tractability 근거를 자격과 사유 코드로 변환 | 후보 → `eligible` 또는 `exploratory` | 없음 |
| `DaconTargetReasoner.rank` | `reasoner.py` | 후보 순위와 조절 방향 해석 | assessed 후보 → allowlisted shortlist | unavailable, invalid ranking |
| `UniProtProteinProvider.get_reviewed_human_protein` | `providers.py` | DTA용 sequence 검증 | accession → reviewed human protein | unavailable, not verified |

## 데이터 흐름

1. `specified`는 사용자 표적을 Ensembl ID로 확정해 해당 disease-target record만 조회한다.
2. `discover`는 Open Targets direct association 상위 후보를 조회한다.
3. 후보·datasource별 개별 evidence를 조회하고 direct disease와 subtype 근거를 구분한다.
4. association, evidence data type, causal support, target function, biotype,
   small-molecule tractability와 Open Targets API/data release를 typed 계약으로 검증한다.
5. UniProt accession으로 Pharos를 조회해 TDL, target family, novelty, ligand 수와 publication 수를
   별도 `target_maturity` 근거로 보존한다. 이 값으로 causal support나 Open Targets tractability를
   덮어쓰지 않는다.
6. Swiss-Prot accession이 없는 후보는 `reviewed_protein_unavailable`로 제외한다.
7. 결정적 정책은 임상 선례, ligand, pocket 또는 druggable-family 양성 근거가 하나라도 있으면
   `eligible`, 없으면 `exploratory`로 분류한다. false나 누락을 undruggable의 증거로 바꾸지 않는다.
8. causal 정책은 statistical genetics, clinical genetics, somatic, functional과
   clinical validation을 분리하고 방향을 정규화한다.
9. 전체 evidence는 저장하되 모델에는 집계와 후보별 최대 8개의 대표 근거만 전달하고,
   모델은 최대 `EVIDRUG_TARGET_SHORTLIST_LIMIT`개의 ID, 허용 evidence ID와 설명을 반환한다.
10. 서버는 ID, evidence allowlist, 조절 방향과 후보 수를 재검증한 뒤 UniProt sequence를 검증한다.
11. 개별 sequence 검증 실패는 해당 후보만 제외한다. provider 전체 장애는 typed failure다.
12. primary, alternatives, exclusions, gaps, warnings와 출처는 `agent_runs.output_json`에 저장한다.

SMILES는 이 단계의 LLM prompt에 전달하지 않는다. 현재 Target 단계는 소분자 modality 후보를
만들며, 실제 compound-target 적합성은 후속 DTA가 후보별로 계산해야 한다.

## 계약과 공개 API

`TargetHypothesisResult`는 다음을 보존한다.

- `modality=small_molecule`
- 검증된 primary와 최대 4개의 alternatives
- 후보별 association/data type score와 원 tractability assessment
- 후보별 `supported | conflicting | unknown` causal support, reason code, LLM이 선택한 evidence ID와
  개별 evidence
- direct disease/subtype 범위, datasource와 정규화된 target/trait 방향
- `eligible | exploratory | ineligible`, 정책 사유 코드와 조절 방향
- UniProt accession, sequence, SHA-256
- 제외 후보와 `reviewed_protein_unavailable | not_shortlisted |
  target_sequence_not_verified` 사유
- Open Targets API/data release와 조회 시각

`GET /api/v1/analyses/{analysis_id}`의 `target_prioritization`은 sequence를 제외한 요약만
노출한다. 과거 Target v2 결과는 causal support를 `unknown`으로 안전하게 투영하며, Target 결과가
없거나 계약을 충족하지 않는 JSON이면 `null`이다. 프론트 화면 표현은
Issue #97에서 연결한다.

## 과학적 제약

- association score는 질환 관련성 순위 휴리스틱이며 druggability나 성공 확률이 아니다.
- tractability false 또는 누락은 불가능의 증거가 아니다.
- `eligible`은 Open Targets의 양성 소분자 근거가 있다는 뜻이며 실제 입력 화합물 결합을
  보장하지 않는다.
- clinical precedence는 생물학적 causal support로 계산하지 않는다.
- 치료 방향의 상충은 인과성 자체의 상충으로 바꾸지 않는다.
- 조절 방향은 target/trait 방향 조합으로 제한하며 rationale은 독립 근거가 아니다.
- 유전질환, loss-of-function 또는 높은 genetic association만으로 자동 제외하지 않는다.
- 다른 modality는 아직 지원하지 않는다.

Open Targets, Pharos와 UniProt는 현재 Target 실행의 fixed provider dependency다. Pharos 관측은
후보별 typed prompt context와 결과 provenance에 포함되지만 모델이 endpoint나 인수를 정하지 않는다.
adaptive tool로
공개할 때는 ToolRegistry, admission, 실행 ledger와 예산을 별도 Issue에서 연결한다.

## 실패 코드

| 코드 | 의미 | 재시도 가능 |
| :--- | :--- | :---: |
| `target_not_found` | 검증 가능한 질환-표적 후보 없음 | 아니요 |
| `target_evidence_unavailable` | Open Targets 조회 장애 또는 계약 오류 | 예 |
| `pharos_unavailable` | Pharos 조회 장애 또는 계약 오류 | 예 |
| `target_model_unavailable` | Dacon 모델 호출 장애 | 예 |
| `invalid_target_selection` | ranking 계약, 개수 또는 allowlist 위반 | 아니요 |
| `target_sequence_unavailable` | UniProt provider 장애 | 예 |
| `target_sequence_not_verified` | 모든 shortlist sequence 검증 실패 | 아니요 |

## 설정과 검증

- `EVIDRUG_OPEN_TARGETS_GRAPHQL_URL`
- `EVIDRUG_UNIPROT_BASE_URL`
- `EVIDRUG_PHAROS_GRAPHQL_URL`
- `EVIDRUG_PHAROS_ENABLED` (기본 `true`; fixed Pharos 포함/제외 ablation)
- `EVIDRUG_TARGET_LOOKUP_TIMEOUT_SECONDS`
- `EVIDRUG_TARGET_CANDIDATE_LIMIT`
- `EVIDRUG_TARGET_SHORTLIST_LIMIT`

정상 `discover`의 외부 요청 수는 Open Targets association 1회와 evidence 1회, 후보별 Pharos 1회,
Dacon 1회, shortlist 후보별 UniProt 1회다. `specified` alias resolution은 Open Targets search 1회를
추가한다. Dacon SDK 자동 재시도와 숨은 fallback은 사용하지 않는다.

```sh
uv run --no-env-file pytest tests/test_target_hypothesis.py tests/test_orchestration.py
```

Open Targets Platform data는 CC0 1.0이며 원 통합 데이터의 조건을 함께 확인해야 한다.
UniProt의 copyrightable database content는 CC BY 4.0이다. API가 보고한 release와 조회 시각은
저장하지만 장기 재현용 snapshot은 아직 만들지 않는다.

관련: Issue #95,
[Target v2 기능 명세](../../../../docs/features/target-prioritization-v2.md),
[causal support 기능 명세](../../../../docs/features/target-causal-support.md).
