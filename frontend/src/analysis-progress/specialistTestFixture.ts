import fixture from './fixtures/specialist-api-summary.json'
import { parseSpecialistResults, type SpecialistResults } from './specialistResults'

/** breast cancer / CDK4 / palbociclib 실제 공개 응답의 ID를 치환한 테스트 자료. */
export function savedFixture() { return parseSpecialistResults(structuredClone(fixture)) }

/** DTA 부분 실패와 Decision 표시 검증용 합성 데이터. 과학적 판정 자료가 아니다. */
export function partialFixture(): SpecialistResults {
  const data = savedFixture()
  data.status = 'partial_failure'
  data.dta = {
    ...data.dta, status: 'partial_failure', projection_status: 'available', error_code: 'reasoning_invalid_output',
    result: {
      assay_policy_version: 'legacy',
      source_target_run_id: data.admet.run_id!,
      candidates: [{ approved_symbol: 'CDK4', ensembl_id: 'ENSG00000135446', uniprot_accession: 'P11802',
        status: 'succeeded', error_code: null, tool_call_id: data.dta.run_id,
        model: { provider: 'test', model_id: 'synthetic-dta', version: 'test-v1' },
        observations: [{ score_type: 'predicted_pkd', value: 4.811026573181152, unit: '-log10(Kd [M])' }],
        model_runs: [],
        experimental_evidence: [], evidence_assessment: null, evidence_errors: [],
      }],
      interpretation: null, interpretation_note: '테스트: 해석 실패, 예측값 보존.',
    },
  }
  data.decision = {
    ...data.decision, status: 'completed', projection_status: 'available', error_code: null,
    result: { verdict: 'conditional_go', rationale: '화면 테스트용 합성 연구 우선순위입니다.',
      headline: null, assessment: null, key_strengths: null, key_concerns: null,
      server_metadata: null, context_version: null,
      decision_phase: 'final', recall_request: null,
      used_evidence_ids: ['test-evidence'], conflicts: ['검증 필요'], gaps: ['해석 공백'],
      next_actions: [{ status: 'proposed',
        action: '결합 예측 차이를 확인할 정량 결합 실험을 제안합니다.',
        rationale: '두 모델의 차이가 현재 조건부 판정의 핵심 불확실성이기 때문입니다.',
        decision_impact: '결합이 일관되게 확인되면 우선순위를 높이고, 재현되지 않으면 낮춥니다.' }],
      missing_stages: [], source_run_ids: [data.dta.run_id!], scope: 'research_prioritization_only' },
  }
  return data
}
