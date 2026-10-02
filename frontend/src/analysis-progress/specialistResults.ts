import { AnalysisRequestError, requestAnalysisData } from './api'

/** #111 공개 DTO만 복사한다. 내부 원문/서열 등 추가 필드는 보존하지 않는다. */
export function parseSpecialistResults(value: unknown) {
  const data = record(value)
  return {
    analysis_id: uuid(data.analysis_id),
    status: choice(data.status, ['queued', 'running', 'completed', 'partial_failure', 'failed']),
    admet: run(data.admet, admet),
    dta: run(data.dta, dta),
    decision: run(data.decision, decision),
    calls: data.calls === undefined ? [] : list(data.calls, agentCall),
  }
}
export type SpecialistResults = ReturnType<typeof parseSpecialistResults>
export type SpecialistRun<T> = ReturnType<typeof run<T>>

export async function readSpecialistResults(id: string, signal?: AbortSignal) {
  const expected = uuid(id)
  const result = parseSpecialistResults(await requestAnalysisData(
    `/api/v1/analyses/${encodeURIComponent(expected)}/results`,
    { method: 'GET', signal, cache: 'no-store', headers: { Accept: 'application/json' } },
  ))
  if (result.analysis_id !== expected) invalid()
  return result
}

function run<T>(value: unknown, parse: (value: unknown) => T) {
  const data = record(value)
  const result = nullable(data.result, parse)
  const projection = choice(data.projection_status, ['available', 'unavailable', 'invalid'])
  const status = nullable(data.status, (v) => choice(v, [
    'running', 'completed', 'partial_failure', 'failed', 'skipped',
  ]))
  const runId = nullable(data.run_id, uuid)
  if ((projection === 'available') !== (result !== null)) invalid()
  if (result !== null && (!runId || !['completed', 'partial_failure'].includes(status ?? ''))) invalid()
  return {
    run_id: runId, status, projection_status: projection, result,
    error_code: nullable(data.error_code, text),
    warning_codes: list(data.warning_codes, text),
    token_usage: nullable(data.token_usage, (v) => {
      const usage = record(v)
      const input = count(usage.input_tokens), output = count(usage.output_tokens)
      const total = count(usage.total_tokens)
      if (total < input + output) invalid()
      return { input_tokens: input, output_tokens: output, total_tokens: total }
    }),
  }
}

function interpretation(value: unknown) {
  const data = record(value)
  return {
    summary: text(data.summary),
    used_evidence_ids: list(data.used_evidence_ids, text),
    limitations: list(data.limitations, text),
  }
}

function admet(value: unknown) {
  const data = record(value)
  const endpoints = list(data.endpoints, (v) => {
    const item = record(v)
    const task = choice(item.task_type, ['classification', 'regression'])
    const prediction = finite(item.value)
    const percentile = finite(item.drugbank_approved_percentile)
    if (percentile < 0 || percentile > 100 || (task === 'classification' && (prediction < 0 || prediction > 1))) invalid()
    return {
      endpoint_id: text(item.endpoint_id), name: text(item.name), category: text(item.category),
      task_type: task, value: prediction, units: nullable(item.units, text),
      drugbank_approved_percentile: percentile, species: nullable(item.species, text),
      source_url: nullable(item.source_url, text),
    }
  })
  const selected = count(data.selected_endpoint_count), catalog = count(data.catalog_endpoint_count)
  if (selected !== endpoints.length || selected > catalog || typeof data.selection_is_subset !== 'boolean'
      || data.selection_is_subset !== (selected < catalog)) invalid()
  return {
    source_run_id: uuid(data.source_run_id), source_tool_call_id: uuid(data.source_tool_call_id),
    tool_version: text(data.tool_version), reference_population: text(data.reference_population),
    selected_endpoint_count: selected, catalog_endpoint_count: catalog,
    selection_is_subset: data.selection_is_subset,
    missing_endpoints: list(data.missing_endpoints, text), endpoints,
    toxicity_axes: optional(data.toxicity_axes, toxicityAxes),
    interpretation: nullable(data.interpretation, interpretation),
    interpretation_note: text(data.interpretation_note), limitations: list(data.limitations, text),
  }
}

function dta(value: unknown) {
  const data = record(value)
  const candidates = list(data.candidates, (v) => {
    const item = record(v)
    const status = choice(item.status, ['succeeded', 'failed', 'skipped'])
    const observations = list(item.observations, dtaScore)
    const model = nullable(item.model, dtaModel)
    const error = nullable(item.error_code, text)
    const tool = nullable(item.tool_call_id, uuid)
    const modelRuns = item.model_runs === undefined ? [] : list(item.model_runs, dtaModelRun)
    const experimentalEvidence = item.experimental_evidence === undefined
      ? [] : list(item.experimental_evidence, dtaAssayEvidence, 500)
    const evidenceAssessment = item.evidence_assessment === undefined
      ? null : nullable(item.evidence_assessment, dtaEvidenceAssessment)
    const evidenceErrors = item.evidence_errors === undefined ? [] : list(item.evidence_errors, text)
    if (status === 'succeeded' ? !model || !tool || !observations.length || error !== null
      : observations.length > 0 || model !== null || error === null) invalid()
    if (modelRuns.length && status === 'succeeded' && !modelRuns.some((run) =>
      run.status === 'succeeded' && run.tool_call_id === tool && run.model?.model_id === model?.model_id
    )) invalid()
    if (status !== 'succeeded' && modelRuns.some((run) => run.status === 'succeeded')) invalid()
    return {
      ensembl_id: text(item.ensembl_id), approved_symbol: text(item.approved_symbol),
      uniprot_accession: text(item.uniprot_accession), status, model, observations,
      error_code: error, tool_call_id: tool, model_runs: modelRuns,
      experimental_evidence: experimentalEvidence,
      evidence_assessment: evidenceAssessment,
      evidence_errors: evidenceErrors,
    }
  })
  if (!candidates.length || candidates.length > 5) invalid()
  return {
    source_target_run_id: uuid(data.source_target_run_id), candidates,
    assay_policy_version: data.assay_policy_version === undefined ? 'legacy' : text(data.assay_policy_version),
    interpretation: nullable(data.interpretation, interpretation),
    interpretation_note: text(data.interpretation_note),
  }
}

function dtaAssayEvidence(value: unknown) {
  const evidence = record(value)
  const kind = choice(evidence.kind, ['quantitative', 'qualitative'])
  const endpoint = nullable(evidence.endpoint, (v) => choice(v, ['Kd', 'Ki', 'IC50']))
  const measured = nullable(evidence.value, finite)
  const unit = nullable(evidence.unit, (v) => choice(v, ['pM', 'nM', 'uM', 'mM', 'M']))
  const outcome = nullable(evidence.qualitative_outcome, text)
  if (kind === 'quantitative' ? endpoint === null || measured === null || unit === null || outcome !== null
    : endpoint !== null || measured !== null || unit !== null || outcome === null) invalid()
  return {
    source: choice(evidence.source, ['bindingdb', 'chembl', 'pubchem']),
    source_record_id: text(evidence.source_record_id), kind, endpoint, value: measured, unit,
    qualitative_outcome: outcome,
    assay_description: nullable(evidence.assay_description, text),
    doi: nullable(evidence.doi, text), pmid: nullable(evidence.pmid, text),
  }
}

function dtaEvidenceAssessment(value: unknown) {
  const assessment = record(value)
  return {
    status: choice(assessment.status, [
      'CONCORDANT', 'DECISION_RELEVANT_DISAGREEMENT', 'PREDICTION_EXPERIMENT_CONFLICT',
      'EXPERIMENT_SOURCE_CONFLICT', 'INSUFFICIENT_EXPERIMENTAL_EVIDENCE', 'NOT_COMPARABLE',
    ]),
    recall_trigger: nullable(assessment.recall_trigger, text),
    pubchem_requested: flag(assessment.pubchem_requested),
    pubchem_executed: flag(assessment.pubchem_executed),
    pubchem_resolved: flag(assessment.pubchem_resolved),
    limitations: list(assessment.limitations, text),
    region_status: optional(assessment.region_status, (v) => choice(v, [
      'same_region_meets_reference', 'same_region_below_reference',
      'split_across_reference', 'insufficient_model_results',
    ])),
    experimental_binding_support: optional(assessment.experimental_binding_support, flag),
    pubchem_execution_issue: optional(assessment.pubchem_execution_issue, (v) => choice(v, [
      'provider_disabled', 'tool_budget_exhausted', 'execution_failed',
    ])),
    criterion: optional(assessment.criterion, (v) => {
      const c = record(v)
      return { endpoint: choice(c.endpoint, ['Kd', 'Ki', 'IC50']), maximum_value: finite(c.maximum_value), unit: text(c.unit) }
    }),
  }
}

function dtaScore(value: unknown) {
  const score = record(value)
  return { score_type: text(score.score_type), value: finite(score.value), unit: text(score.unit) }
}

function dtaModel(value: unknown) {
  const model = record(value)
  return { provider: text(model.provider), model_id: text(model.model_id), version: text(model.version) }
}

function dtaModelRun(value: unknown) {
  const run = record(value)
  const status = choice(run.status, ['succeeded', 'failed', 'skipped'])
  const model = nullable(run.model, dtaModel)
  const observations = list(run.observations, dtaScore)
  const toolCallId = nullable(run.tool_call_id, uuid)
  const error = nullable(run.error_code, text)
  if (status === 'succeeded' ? !model || !toolCallId || !observations.length || error !== null
    : model !== null || observations.length > 0 || error === null) invalid()
  if (status === 'skipped' && toolCallId !== null) invalid()
  return {
    tool_id: text(run.tool_id), tool_call_id: toolCallId, status, model, observations,
    error_code: error, duration_ms: count(run.duration_ms),
  }
}

function decision(value: unknown) {
  const data = record(value)
  const nextActions = data.next_actions === undefined ? [] : list(data.next_actions, (value) => {
    const action = record(value)
    return {
      status: choice(action.status, ['proposed']),
      action: text(action.action),
      rationale: text(action.rationale),
      decision_impact: text(action.decision_impact),
    }
  })
  if (nextActions.length > 3) invalid()
  return {
    headline: optional(data.headline, text),
    decision_phase: data.decision_phase === undefined ? 'final' as const : choice(data.decision_phase, ['final', 'request_recall']),
    recall_request: optional(data.recall_request, record),
    assessment: optional(data.assessment, decisionAreas),
    key_strengths: optional(data.key_strengths, (v) => list(v, text, 3)),
    key_concerns: optional(data.key_concerns, (v) => list(v, text, 3)),
    server_metadata: optional(data.server_metadata, decisionMetadata),
    context_version: optional(data.context_version, text),
    verdict: choice(data.verdict, ['go', 'conditional_go', 'no_go']),
    rationale: text(data.rationale), used_evidence_ids: list(data.used_evidence_ids, text),
    conflicts: list(data.conflicts, text), gaps: list(data.gaps, text),
    next_actions: nextActions,
    source_run_ids: list(data.source_run_ids, uuid),
    missing_stages: list(data.missing_stages, (v) => choice(v, ['target_hypothesis', 'admet', 'dta', 'decision'])),
    scope: choice(data.scope, ['research_prioritization_only']),
  }
}

function optional<T>(value: unknown, parser: (v: unknown) => T): T | null {
  return value === undefined || value === null ? null : parser(value)
}

function toxicityAxes(value: unknown) {
  const data = record(value)
  return {
    policy_version: text(data.policy_version),
    axes: list(data.axes, (v) => {
      const axis = record(v)
      return {
        axis: choice(axis.axis, ['liver_injury', 'cardiac_ion_channel', 'mutagenicity']),
        endpoint_id: text(axis.endpoint_id), value: nullable(axis.value, finite),
        drugbank_approved_percentile: nullable(axis.drugbank_approved_percentile, finite),
        priority_status: choice(axis.priority_status, ['priority_check', 'not_priority', 'missing']),
        source_tool_call_id: nullable(axis.source_tool_call_id, uuid),
      }
    }, 3),
  }
}

function decisionAreas(value: unknown) {
  const data = record(value)
  function area(value: unknown, isAdme = false) {
    const data = record(value)
    return {
      status: choice(data.status, isAdme ? ['informative', 'unavailable'] : ['supported', 'unresolved', 'unavailable']),
      summary: text(data.summary),
      key_values: list(data.key_values, (v) => {
        const item = record(v)
        return { label: text(item.label), value: finite(item.value), unit: nullable(item.unit, text) }
      }, 6),
    }
  }
  return { target: area(data.target), dta: area(data.dta), adme: area(data.adme, true), safety: area(data.safety) }
}

function decisionMetadata(value: unknown) {
  const data = record(value)
  return {
    lead_candidate: nullable(data.lead_candidate, (v) => {
      const lead = record(v)
      return { ensembl_id: text(lead.ensembl_id), symbol: text(lead.symbol), rule: text(lead.rule) }
    }),
    candidate_gates: list(data.candidate_gates, (v) => {
      const item = record(v)
      return { ensembl_id: text(item.ensembl_id), symbol: text(item.symbol),
        target_supported: flag(item.target_supported), dta_supported: flag(item.dta_supported) }
    }, 5),
    confirm_channels: list(data.confirm_channels, text),
    go_restrictions: list(data.go_restrictions, (v) => {
      const item = record(v)
      return { stage: text(item.stage), cause: text(item.cause) }
    }),
    expected_area_statuses: record(data.expected_area_statuses),
    safety_gate_baseline: choice(data.safety_gate_baseline, ['supported', 'unresolved', 'unavailable']),
    safety_gate_final: choice(data.safety_gate_final, ['supported', 'unresolved', 'unavailable']),
    prompt_version: text(data.prompt_version), toxicity_policy_version: text(data.toxicity_policy_version),
    assay_policy_version: text(data.assay_policy_version),
  }
}

function cardiacRecall(value: unknown) {
  const data = record(value)
  return {
    source_tool_call_id: uuid(data.source_tool_call_id),
    tool_call_id: uuid(data.tool_call_id),
    tool_version: text(data.tool_version),
    execution_mode: choice(data.execution_mode, ['live', 'replay']),
    predictions: list(data.predictions, (value) => {
      const prediction = record(value)
      const probability = finite(prediction.class_probability)
      if (probability < 0 || probability > 1) invalid()
      return {
        channel: choice(prediction.channel, ['herg', 'nav1_5', 'cav1_2']),
        label: choice(prediction.label, ['positive', 'negative']),
        class_probability: probability,
      }
    }),
    limitations: list(data.limitations, text),
  }
}

function agentCall(value: unknown) {
  const data = record(value)
  const resultKind = choice(data.result_kind, [
    'target_reference', 'admet_baseline', 'cardiac_ion_channel', 'dta', 'decision',
  ])
  const projection = choice(data.projection_status, [
    'available', 'unavailable', 'invalid', 'not_projected',
  ])
  const result = resultKind === 'cardiac_ion_channel'
    ? nullable(data.result, cardiacRecall) : null
  if (resultKind === 'cardiac_ion_channel' && (projection === 'available') !== (result !== null)) invalid()
  const request = nullable(data.request, (value) => {
    const request = record(value)
    return {
      requesting_run_id: uuid(request.requesting_run_id),
      gap_kind: text(request.gap_kind),
      objective: text(request.objective),
      reason_code: text(request.reason_code),
      required_endpoints: list(request.required_endpoints, text),
    }
  })
  const callNumber = count(data.call_number)
  if (callNumber < 1) invalid()
  return {
    agent_name: choice(data.agent_name, ['target_hypothesis', 'admet', 'dta', 'decision']),
    call_number: callNumber,
    run_id: uuid(data.run_id),
    status: nullable(data.status, (value) => choice(value, [
      'running', 'completed', 'partial_failure', 'failed', 'skipped',
    ])),
    purpose: choice(data.purpose, ['initial', 'evidence_followup', 'reassessment', 'unverified']),
    triggering_run_id: nullable(data.triggering_run_id, uuid),
    responds_to_run_id: nullable(data.responds_to_run_id, uuid),
    request,
    response_run_id: nullable(data.response_run_id, uuid),
    result_kind: resultKind,
    projection_status: projection,
    cardiac_result: result,
    error_code: nullable(data.error_code, text),
  }
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalid()
  return value as Record<string, unknown>
}
function text(value: unknown): string {
  if (typeof value !== 'string' || !value.length || value.length > 10000) invalid()
  return value
}
function finite(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) invalid()
  return value
}
function flag(value: unknown): boolean {
  if (typeof value !== 'boolean') invalid()
  return value
}
function count(value: unknown): number {
  const n = finite(value)
  if (!Number.isSafeInteger(n) || n < 0) invalid()
  return n
}
function list<T>(value: unknown, parse: (item: unknown) => T, maximumLength = 100): T[] {
  if (!Array.isArray(value) || value.length > maximumLength) invalid()
  return value.map(parse)
}
function nullable<T>(value: unknown, parse: (item: unknown) => T): T | null {
  return value === null ? null : parse(value)
}
function choice<const T extends readonly string[]>(value: unknown, choices: T): T[number] {
  if (typeof value !== 'string' || !choices.includes(value)) invalid()
  return value as T[number]
}
export function isAnalysisId(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
}
function uuid(value: unknown): string {
  const id = text(value)
  if (!isAnalysisId(id)) invalid()
  return id.toLowerCase()
}
function invalid(): never {
  throw new AnalysisRequestError('unexpected_response', 'Specialist results did not match the public contract.')
}
