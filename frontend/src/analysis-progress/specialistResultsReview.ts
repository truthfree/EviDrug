import type { Analysis } from './api'
import type { SpecialistResults } from './specialistResults'
import { formatScientificNumber } from './scientificNumber'

export function buildSpecialistResultsReview(data: SpecialistResults, analysis: Analysis) {
  const admet = data.admet.result
  const dta = data.dta.result
  const decision = data.decision.result
  const cardiacFollowups = data.calls.flatMap((call) => call.cardiac_result ? [{
    predictions: call.cardiac_result.predictions.map((prediction) => ({
      ...prediction,
      display_probability: formatScientificNumber(prediction.class_probability),
    })),
    limitations: call.cardiac_result.limitations,
    tool_version: call.cardiac_result.tool_version,
    execution_mode: call.cardiac_result.execution_mode,
  }] : [])
  const evidenceManifest = decision
    ? buildDecisionEvidenceManifest(decision.used_evidence_ids, analysis, admet, dta, cardiacFollowups)
    : []
  const missingEvidenceIds = evidenceManifest
    .filter((item) => item.status === 'missing')
    .map((item) => item.evidence_id)

  return {
    execution_errors: [data.admet, data.dta, data.decision].flatMap((run) => run.error_code ? [{
      code: run.error_code,
      message: run.error_code === 'decision_dta_assay_policy_mismatch'
        ? '과거 결합 근거 조회 정책의 결과는 새 판정에 사용할 수 없습니다. DTA부터 다시 실행해야 합니다.'
        : run.error_code,
    }] : []),
    input_context: {
      disease_id: analysis.input.disease_id,
      disease_name: analysis.input.disease_name,
      target_mode: analysis.input.target_mode,
      target_name: analysis.input.target_name,
      canonical_smiles: analysis.input.canonical_smiles,
      potency_criterion: analysis.input.potency_criterion ?? null,
    },
    agent_results: {
      target_hypothesis: analysis.target_prioritization ? {
        result: analysis.target_prioritization,
        evidence: collectTargetEvidence(analysis.target_prioritization),
        limitations: [
          '표적 우선순위는 연구 가설이며 임상 효능이나 치료 권고를 의미하지 않습니다.',
        ],
      } : null,
      admet: admet ? {
        result: {
          toxicity_axes: admet.toxicity_axes,
          endpoints: admet.endpoints.map((endpoint) => ({
            ...endpoint,
            display_value: formatScientificNumber(endpoint.value),
            display_percentile: formatScientificNumber(endpoint.drugbank_approved_percentile),
          })),
          selected_endpoint_count: admet.selected_endpoint_count,
          catalog_endpoint_count: admet.catalog_endpoint_count,
          missing_endpoints: admet.missing_endpoints,
          interpretation: admet.interpretation?.summary ?? null,
        },
        evidence: {
          used_evidence_ids: admet.interpretation?.used_evidence_ids ?? [],
          sources: admet.endpoints.flatMap((endpoint) => endpoint.source_url ? [{
            endpoint_id: endpoint.endpoint_id,
            source_url: endpoint.source_url,
          }] : []),
          tool_version: admet.tool_version,
          reference_population: admet.reference_population,
        },
        limitations: unique([
          ...admet.limitations,
          ...(admet.interpretation?.limitations ?? []),
          admet.interpretation_note,
        ]),
      } : null,
      dta: dta ? {
        result: {
          assay_policy_version: dta.assay_policy_version,
          candidates: dta.candidates.map((candidate) => ({
            ensembl_id: candidate.ensembl_id,
            approved_symbol: candidate.approved_symbol,
            uniprot_accession: candidate.uniprot_accession,
            status: candidate.status,
            error_code: candidate.error_code,
            experimental_evidence: candidate.experimental_evidence.map((evidence) => ({
              ...evidence,
              display_value: evidence.value === null
                ? null
                : formatScientificNumber(evidence.value),
            })),
            evidence_assessment: candidate.evidence_assessment,
            evidence_errors: candidate.evidence_errors,
            predictions: candidate.model_runs.length > 0
              ? candidate.model_runs.map((run) => ({
                status: run.status,
                model: run.model,
                observations: run.observations.map((observation) => ({
                  ...observation,
                  display_value: formatScientificNumber(observation.value),
                })),
                error_code: run.error_code,
              }))
              : [{
                status: candidate.status,
                model: candidate.model,
                observations: candidate.observations.map((observation) => ({
                  ...observation,
                  display_value: formatScientificNumber(observation.value),
                })),
                error_code: candidate.error_code,
              }],
          })),
          interpretation: dta.interpretation?.summary ?? null,
        },
        evidence: dta.interpretation?.used_evidence_ids ?? [],
        limitations: unique([
          ...(dta.interpretation?.limitations ?? []),
          dta.interpretation_note,
        ]),
      } : null,
      decision: decision ? {
        result: {
          headline: decision.headline,
          decision_phase: decision.decision_phase,
          recall_request: decision.recall_request,
          assessment: decision.assessment,
          key_strengths: decision.key_strengths,
          key_concerns: decision.key_concerns,
          server_metadata: decision.server_metadata,
          context_version: decision.context_version,
          verdict: decision.verdict,
          rationale: decision.rationale,
          scope: decision.scope,
          next_actions: decision.next_actions,
        },
        evidence: decision.used_evidence_ids,
        evidence_manifest: evidenceManifest,
        evidence_audit: {
          status: missingEvidenceIds.length === 0 ? 'complete' : 'audit_incomplete',
          missing_evidence_ids: missingEvidenceIds,
        },
        cardiac_followups: cardiacFollowups,
        limitations: {
          conflicts: decision.conflicts,
          gaps: decision.gaps,
          missing_stages: decision.missing_stages,
        },
      } : null,
    },
  }
}

type AdmetResult = SpecialistResults['admet']['result']
type DtaResult = SpecialistResults['dta']['result']
type CardiacFollowup = {
  predictions: Array<{
    channel: 'herg' | 'nav1_5' | 'cav1_2'
    label: 'positive' | 'negative'
    class_probability: number
    display_probability: string
  }>
  limitations: string[]
  tool_version: string
  execution_mode: 'live' | 'replay'
}
type EvidenceManifestItem = {
  evidence_id: string
  status: 'available' | 'missing'
  export_path: string | null
}

function buildDecisionEvidenceManifest(
  evidenceIds: string[],
  analysis: Analysis,
  admet: AdmetResult,
  dta: DtaResult,
  cardiacFollowups: CardiacFollowup[],
): EvidenceManifestItem[] {
  const targetIds = new Set(
    analysis.target_prioritization
      ? [analysis.target_prioritization.primary, ...analysis.target_prioritization.alternatives]
        .map((candidate) => candidate.ensembl_id)
      : [],
  )
  const dtaIds = new Set(dta?.candidates.map((candidate) => candidate.ensembl_id) ?? [])

  return evidenceIds.map((evidenceId) => {
    const targetId = evidenceId.startsWith('target:') ? evidenceId.slice('target:'.length) : null
    const dtaId = evidenceId.startsWith('dta:') ? evidenceId.slice('dta:'.length) : null
    const exportPath = targetId && targetIds.has(targetId)
      ? `agent_results.target_hypothesis.result[ensembl_id=${targetId}]`
      : evidenceId === 'admet:context' && admet
        ? 'agent_results.admet'
        : dtaId && dtaId !== 'interpretation' && dtaIds.has(dtaId)
          ? `agent_results.dta.result.candidates[ensembl_id=${dtaId}]`
          : evidenceId === 'dta:interpretation' && dta
            ? 'agent_results.dta'
            : evidenceId === 'admet:cardiac_ion_channels' && cardiacFollowups.length > 0
              ? 'agent_results.decision.cardiac_followups'
              : null
    return {
      evidence_id: evidenceId,
      status: exportPath ? 'available' as const : 'missing' as const,
      export_path: exportPath,
    }
  })
}

export function buildSpecialistResultsReviewText(data: SpecialistResults, analysis: Analysis): string {
  return `${JSON.stringify(buildSpecialistResultsReview(data, analysis), null, 2)}\n`
}

export function downloadSpecialistResultsReview(data: SpecialistResults, analysis: Analysis): void {
  const contents = buildSpecialistResultsReviewText(data, analysis)
  const url = URL.createObjectURL(new Blob([contents], { type: 'application/json;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `evidrug-results-${data.analysis_id}.json`
  link.hidden = true
  document.body.append(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

function collectTargetEvidence(target: NonNullable<Analysis['target_prioritization']>) {
  return [target.primary, ...target.alternatives].map((candidate) => ({
    ensembl_id: candidate.ensembl_id,
    used_evidence_ids: candidate.causal_evidence_ids,
    causal_evidence: candidate.causal_support.evidence,
  }))
}

function unique(values: string[]): string[] {
  return [...new Set(values)]
}
