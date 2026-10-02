import fixture from './fixtures/cdk4-api-summary.json'
import { parseTargetPrioritization } from './targetPrioritization'

/** CDK4 공개 응답에 UI 상태 검토용 합성 대안을 더한다. 실시간 과학 결과가 아니다. */
export const targetDemo = parseTargetPrioritization({
  ...fixture,
  source_version: 'demo-fixture-v1',
  alternatives: [
    {
      ...fixture.primary,
      rank: 2,
      ensembl_id: 'ENSG00000121879',
      approved_symbol: 'PIK3CA',
      uniprot_accession: 'P42336',
      association_score: 0.5,
      eligibility: 'exploratory',
      eligibility_reason_codes: ['small_molecule_tractability_evidence_missing'],
      tractability_assessments: [],
      pharos_evidence: null,
      modulation_action: 'unknown',
      prioritization_rationale:
        '데모용 합성 대안입니다. 실제 PIK3CA의 접근성 평가가 아니라 추가 검증 상태의 화면 예시입니다.',
      causal_rationale: '데모용 합성 근거는 서로 다른 방향을 가리키므로 판단을 보류합니다.',
      causal_support: {
        status: 'supported',
        reason_codes: ['therapeutic_direction_conflicting'],
        biological_evidence_count: 2,
        clinical_validation_count: 0,
        therapeutic_direction: 'unknown',
        evidence: ['gain_of_function', 'loss_of_function'].map((direction, index) => ({
          evidence_id: `demo-evidence-${index + 1}`,
          datasource_id: 'demo-provider',
          datatype_id: 'somatic_mutation',
          axis: 'somatic',
          score: 0.5,
          disease_id: 'MONDO_0007254',
          disease_name: 'breast cancer',
          disease_scope: 'direct',
          direction_on_target: direction,
          direction_on_trait: 'risk',
          target_role: null,
          confidence: null,
          significant_driver_methods: [],
        })),
      },
      causal_evidence_ids: ['demo-evidence-1', 'demo-evidence-2'],
    },
  ],
  excluded_candidates: [
    {
      ensembl_id: 'ENSG00000091831',
      approved_symbol: 'ESR1',
      association_score: 0.5,
      reason_code: 'not_shortlisted',
    },
  ],
})
