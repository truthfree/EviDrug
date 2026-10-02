# Target causal support

## 목적

Target Prioritization이 높은 association score나 승인 약물 존재를 질환 인과성으로 오해하지
않도록 Open Targets의 개별 evidence를 조회하고 다음 네 축을 분리한다.

1. `association_score`: 질환 관련성 순위
2. `causal_support`: 통계 유전·임상 유전·체세포 driver·기능 perturbation 근거
3. `tractability`: 소분자 표적화 근거
4. `therapeutic_direction`: evidence 방향으로부터 얻은 조절 방향

`clinical_precedence`는 별도의 `clinical_validation` 축이다. 승인 약물이나 임상시험이 있다는
사실은 치료 방향을 보강할 수 있지만 생물학적 인과 근거 개수에는 포함하지 않는다.

## Open Targets 조회

association 후보를 확정한 뒤 한 GraphQL 요청에 후보·datasource별 alias를 만들어 각 source의
상위 evidence를 독립적으로 조회한다. 특정 target이나 datasource의 많은 evidence가 다른 후보의
근거를 밀어내지 않도록 전체 후보를 하나의 공용 page로 조회하지 않는다.

| 축 | datasource |
| :--- | :--- |
| statistical genetics | `gwas_credible_sets`, `gene_burden` |
| clinical genetics | `eva`, `gene2phenotype`, `orphanet` |
| somatic | `eva_somatic`, `cancer_gene_census`, `intogen` |
| functional | `crispr` |
| clinical validation | `clinical_precedence` |

`enableIndirect=true`로 받은 각 evidence의 실제 disease ID가 입력 disease ID와 같으면 `direct`,
하위 질환이면 `subtype`으로 보존한다. 원 `evidence_id`, datasource, datatype, score,
`directionOnTarget`, `directionOnTrait`, `targetRole`, confidence와 driver method도 함께 저장한다.

## 결정적 정책

생물학적 evidence가 하나 이상이면 `supported`, 없으면 `unknown`이다. 서로 반대의 조절 방향은
인과성 자체를 부정하는 근거가 아니므로 causal status를 낮추지 않고
`therapeutic_direction=unknown`과 `therapeutic_direction_conflicting`으로 표현한다.
`conflicting`은 향후 명시적인 인과성 지지·반박 근거를 함께 수집할 때를 위해 예약한다.
근거 개수는 확률이나 새로운 종합 점수로 변환하지 않는다.

치료 방향은 다음과 같이 정규화한다.

| Target effect | Trait effect | 방향 |
| :--- | :--- | :--- |
| GoF | risk | inhibit |
| LoF | protective | inhibit |
| LoF | risk | activate |
| GoF | protective | activate |
| 누락 또는 서로 상충 | - | unknown |

clinical validation도 치료 방향 투표에는 참여하지만 causal status에는 참여하지 않는다. 따라서
CDK4처럼 임상 선행 근거만 확인된 경우 `therapeutic_direction=inhibit`가 될 수 있어도
`causal_support.status`는 여전히 `unknown`이다.

## LLM 경계

Prompt v3는 정책과 동일한 함수로 각 근거의 `implied_action`, 전체 `action_counts`와
방향 조합별 매핑을 제공한다. 대표 근거 8개 이내에 억제·활성화 근거를 우선 확보하며,
충돌 후보의 설명은 양쪽 evidence ID를 인용해야 통과한다. GoF/risk와 LoF/protective는
모두 inhibit이므로 이 둘만을 충돌 원인으로 해석해서는 안 된다. ID와 방향 검증은
자연어 의미 전체를 보장하지 않으므로 실제 모델 설명 검토를 병행한다.

전체 evidence는 결과에 보존하되 모델에는 datasource·scope·방향별 집계와 후보당 최대 8개의
대표 evidence만 전달한다. 대표 근거는 각 축을 한 번씩 우선 포함한 뒤 direct disease, 점수와
방향 다양성을 기준으로 결정적으로 선택한다. 모델은 각 추천에 사용한 `causal_evidence_ids`와
한국어 rationale을 반환한다. 검증된 ID는 최종 recommendation과 polling 요약에도 저장해
rationale이 참조한 원 evidence를 감사할 수 있게 한다. 서버는 다음을 다시 검증한다.

- 추천 target ID가 후보 allowlist 안에 있는가
- evidence ID가 해당 target에 제공된 allowlist 안에 있는가
- evidence가 있는데 참조 ID를 비워 두지 않았는가
- modulation action이 결정적 정책의 therapeutic direction과 같은가

자연어 rationale은 해석이며 독립 근거가 아니다. 원 evidence와 reason code가 판단의 기준이다.

## 호환성과 공개 API

새 필드는 기본값을 가져 과거 Target v2 output을 계속 읽을 수 있다. 과거 결과에는
`causal_support.status=unknown`과 `causal_evidence_missing`이 투영된다. polling API는 sequence를
제외하고 causal support, reason code와 개별 evidence를 `target_prioritization`에 포함한다.

## 검증

```sh
cd backend
.venv/bin/pytest -q tests/test_target_hypothesis.py tests/test_analysis_jobs.py

# 실제 Open Targets 최신 API를 사용하는 선택적 smoke
EVIDRUG_RUN_LIVE_PROVIDER_TESTS=1 \
  .venv/bin/pytest -q tests/test_target_hypothesis_live.py
```

live smoke는 breast cancer에서 PIK3CA, BRCA2, CDK4를 각각 specified target으로 조회한다.
외부 릴리스에 따라 evidence 내용은 바뀔 수 있으므로 기본 CI에서는 실행하지 않는다.

관련: Issue #103,
[Target Prioritization v2](./target-prioritization-v2.md).
