import fixtures from './fixtures/decision-v57.json'
import { partialFixture } from './specialistTestFixture'
import { parseSpecialistResults } from './specialistResults'
import type { Analysis } from './api'
import type { TargetAction, TargetRecommendation } from './targetPrioritization'

/** 첨부 개발 사례를 공개 API 형태로 재구성한다. 실제 실행 결과를 대체하지 않는다. */
export function structuredFixture(caseIndex = 0) {
  const fixture = fixtures[caseIndex]
  const data = partialFixture()
  const id = data.analysis_id
  const evidence = fixture.payload.evidence as unknown as Record<string, Record<string, unknown>>
  const admet = evidence['admet:context'] as unknown as {
    context: { rows: Array<Array<string | number | null>>; reference_population: string }
    toxicity_axes: unknown
  }
  const candidates = Object.entries(evidence).flatMap(([key, raw]) => {
    if (!key.startsWith('dta:') || key === 'dta:interpretation') return []
    const source = raw as unknown as {
      approved_symbol: string; uniprot_accession: string; region_status: string
      experimental_binding_support: boolean; pubchem_requested: boolean; pubchem_executed: boolean
      pubchem_execution_issue: string | null
      model_runs: Array<{ tool_id: string; model_id: string; status: string; observations: Array<{ score_type: string; value: number; unit: string }> }>
    }
    const runs = source.model_runs.map((run) => ({ ...run, tool_call_id: id,
      model: { provider: 'fixture', model_id: run.model_id, version: 'fixture-v1' },
      error_code: null, duration_ms: 0 }))
    return [{ ...data.dta.result!.candidates[0], ensembl_id: key.slice(4),
      approved_symbol: source.approved_symbol, uniprot_accession: source.uniprot_accession,
      tool_call_id: id, model: runs[0].model, observations: runs[0].observations, model_runs: runs,
      evidence_assessment: { status: 'INSUFFICIENT_EXPERIMENTAL_EVIDENCE', recall_trigger: null,
        region_status: source.region_status, experimental_binding_support: source.experimental_binding_support,
        pubchem_requested: source.pubchem_requested, pubchem_executed: source.pubchem_executed,
        pubchem_execution_issue: source.pubchem_execution_issue, pubchem_resolved: false, limitations: [],
        criterion: { endpoint: 'Kd', maximum_value: 100, unit: 'nM' } } }]
  })
  const parsed = parseSpecialistResults({ ...data, status: 'completed',
    admet: { ...data.admet, result: { ...data.admet.result, toxicity_axes: admet.toxicity_axes,
      reference_population: admet.context.reference_population,
      selected_endpoint_count: admet.context.rows.length, catalog_endpoint_count: admet.context.rows.length,
      selection_is_subset: false, endpoints: admet.context.rows.map((row) => ({
        endpoint_id: row[0], name: row[1], category: row[2], task_type: row[3], value: row[4],
        units: row[5], drugbank_approved_percentile: row[6], species: row[7], source_url: row[8],
      })) } },
    dta: { ...data.dta, status: 'completed', error_code: null,
      result: { ...data.dta.result, assay_policy_version: 'pubchem-recall-v1', candidates } },
    decision: { ...data.decision, result: { ...data.decision.result, ...fixture.output,
      server_metadata: fixture.metadata, context_version: 'decision-context-v5.7' } },
  })
  const targetCandidates: TargetRecommendation[] = Object.entries(evidence).flatMap(([key, raw], i) => {
    if (!key.startsWith('target:')) return []
    return [{ rank: i + 1, ensembl_id: key.slice(7), approved_symbol: String(raw.symbol),
      uniprot_accession: 'P11802', association_score: Number(raw.association), eligibility: 'eligible',
      eligibility_reason_codes: [], tractability_assessments: [], pharos_evidence: null,
      causal_support: { status: raw.causal_status as 'supported' | 'conflicting' | 'unknown',
        reason_codes: [], biological_evidence_count: 0, clinical_validation_count: 0,
        therapeutic_direction: raw.policy_direction as TargetAction, evidence: [] },
      causal_evidence_ids: [], modulation_action: raw.policy_direction as TargetAction,
      prioritization_rationale: '첨부 개발 사례입니다.', causal_rationale: '첨부 개발 사례입니다.' }]
  })
  const analysis: Analysis = {
    analysis_id: id, status: 'completed',
    input: { disease_id: 'MONDO_0007254', disease_name: 'breast cancer',
      target_mode: caseIndex === 0 ? 'specified' : 'discover', target_name: caseIndex === 0 ? 'CDK4' : null,
      original_smiles: 'CCO', canonical_smiles: 'CCO' },
    target_prioritization: { modality: 'small_molecule', primary: targetCandidates[0],
      alternatives: targetCandidates.slice(1), excluded_candidates: [], source_version: 'fixture-v1' },
    stages: [], events: [], error_code: null, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
  }
  return { data: parsed, analysis }
}
