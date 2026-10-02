import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseSpecialistResults, readSpecialistResults } from './specialistResults'
import { partialFixture, savedFixture } from './specialistTestFixture'

afterEach(() => vi.unstubAllGlobals())

describe('public specialist results contract', () => {
  it('accepts the captured response and preserves predictions without interpretation', () => {
    expect(savedFixture().admet.result?.selected_endpoint_count).toBe(12)
    expect(savedFixture().calls).toEqual([])
    expect(parseSpecialistResults(partialFixture()).dta.result?.candidates[0].observations[0].value).toBe(4.811026573181152)
    expect(savedFixture().dta.result).toBeNull()
  })
  it('normalizes older DTA results without model-specific runs', () => {
    const data = partialFixture()
    delete (data.dta.result!.candidates[0] as { model_runs?: unknown[] }).model_runs
    expect(parseSpecialistResults(data).dta.result?.candidates[0].model_runs).toEqual([])
  })
  it('normalizes older DTA results without assay evidence fields', () => {
    const data = structuredClone(partialFixture()) as unknown as Record<string, unknown>
    const dta = (data.dta as Record<string, unknown>).result as Record<string, unknown>
    const candidate = (dta.candidates as Array<Record<string, unknown>>)[0]
    delete candidate.experimental_evidence
    delete candidate.evidence_assessment
    delete candidate.evidence_errors
    const parsed = parseSpecialistResults(data).dta.result!.candidates[0]
    expect(parsed.experimental_evidence).toEqual([])
    expect(parsed.evidence_assessment).toBeNull()
    expect(parsed.evidence_errors).toEqual([])
  })
  it('accepts a stored DTA result with more than 100 assay evidence records', () => {
    const data = partialFixture()
    const candidate = data.dta.result!.candidates[0]
    candidate.experimental_evidence = Array.from({ length: 105 }, (_, index) => ({
      source: 'pubchem' as const,
      source_record_id: `pubchem-${index}`,
      kind: 'quantitative' as const,
      endpoint: 'Kd' as const,
      value: 50,
      unit: 'nM' as const,
      qualitative_outcome: null,
      assay_description: null,
      doi: null,
      pmid: null,
    }))
    expect(parseSpecialistResults(data).dta.result?.candidates[0].experimental_evidence)
      .toHaveLength(105)
  })
  it('preserves structured next actions and accepts older Decision results without them', () => {
    const data = partialFixture()
    data.decision.result!.next_actions[0].action = '예측값 4.811026을 외부 실험과 비교합니다.'
    expect(parseSpecialistResults(data).decision.result?.next_actions[0].action).toContain('4.811026')
    const legacy = structuredClone(data) as unknown as Record<string, unknown>
    const decision = (legacy.decision as Record<string, unknown>).result as Record<string, unknown>
    delete decision.next_actions
    expect(parseSpecialistResults(legacy).decision.result?.next_actions).toEqual([])
  })
  it('rejects malformed or excessive Decision next actions', () => {
    const data = partialFixture()
    data.decision.result!.next_actions[0].status = 'completed' as 'proposed'
    expect(() => parseSpecialistResults(data)).toThrow()
    data.decision.result!.next_actions = Array.from({ length: 4 }, () => ({
      status: 'proposed' as const,
      action: '후속 확인을 제안합니다.',
      rationale: '판정 근거를 보완하기 위해 필요합니다.',
      decision_impact: '새 관측에 따라 우선순위를 조정합니다.',
    }))
    expect(() => parseSpecialistResults(data)).toThrow()
  })
  it('validates model-specific DTA runs and discards private fields', () => {
    const data = partialFixture()
    const candidate = data.dta.result!.candidates[0]
    const second = { tool_id: 'dta_mpnn_cnn_bindingdb', tool_call_id: data.analysis_id,
      status: 'succeeded', model: { provider: 'test', model_id: 'MPNN_CNN_BindingDB', version: 'test-v1', artifact_sha256: 'secret' },
      observations: [{ score_type: 'predicted_pkd', value: 6.5901, unit: '-log10(Kd [M])' }],
      error_code: null, duration_ms: 120, target_sequence: 'secret' }
    const first = { tool_id: 'dta', tool_call_id: candidate.tool_call_id, status: 'succeeded',
      model: candidate.model, observations: candidate.observations, error_code: null, duration_ms: 100 }
    const input = { ...data, dta: { ...data.dta, result: { ...data.dta.result,
      candidates: [{ ...candidate, model_runs: [first, second] }] } } }
    const parsed = parseSpecialistResults(input)
    expect(parsed.dta.result!.candidates[0].model_runs.map((run) => run.observations[0].value)).toEqual([4.811026573181152, 6.5901])
    expect(parsed.dta.result!.candidates[0].model_runs[1].model).not.toHaveProperty('artifact_sha256')
    expect(parsed.dta.result!.candidates[0].model_runs[1]).not.toHaveProperty('target_sequence')
    second.observations[0].value = NaN
    expect(() => parseSpecialistResults(input)).toThrow()
  })
  it('preserves public cardiac follow-up call data without leaking extra fields', () => {
    const data = partialFixture()
    const id = data.analysis_id
    const call = {
      agent_name: 'admet', call_number: 2, run_id: id, status: 'completed', purpose: 'evidence_followup',
      triggering_run_id: id, responds_to_run_id: id, response_run_id: id,
      request: { requesting_run_id: id, gap_kind: 'cardiac', objective: '심장 독성 근거 확인',
        reason_code: 'cardiac_evidence_gap', required_endpoints: ['hERG'] },
      result_kind: 'cardiac_ion_channel', projection_status: 'available', error_code: null,
      result: { source_tool_call_id: id, tool_call_id: id, tool_version: 'test', execution_mode: 'live',
        predictions: [{ channel: 'herg', label: 'positive', class_probability: 0.81, secret: 'discard' }],
        limitations: ['모델 예측'], secret: 'discard' },
      secret: 'discard',
    }
    const parsed = parseSpecialistResults({ ...data, calls: [call] })
    expect(parsed.calls[0].cardiac_result?.predictions[0].class_probability).toBe(0.81)
    expect(parsed.calls[0]).not.toHaveProperty('secret')
    expect(parsed.calls[0].cardiac_result).not.toHaveProperty('secret')
    expect(() => parseSpecialistResults({ ...data, calls: [{ ...call, result: {
      ...call.result, predictions: [{ channel: 'herg', label: 'positive', class_probability: 1.5 }],
    } }] })).toThrow()
  })
  it.each(['scope', 'number', 'selection', 'projection', 'units'])('rejects invalid %s', (kind) => {
    const data = partialFixture()
    if (kind === 'scope') Object.assign(data.decision.result!, { scope: 'clinical' })
    if (kind === 'number') data.admet.result!.endpoints[0].value = NaN
    if (kind === 'selection') data.admet.result!.selected_endpoint_count = 49
    if (kind === 'projection') data.admet.projection_status = 'invalid'
    if (kind === 'units') Object.assign(data.dta.result!.candidates[0].observations[0], { unit: null })
    expect(() => parseSpecialistResults(data)).toThrow()
  })
  it('discards non-public fields', () => {
    const data = savedFixture()
    Object.assign(data, { secret: 'hidden' })
    expect(parseSpecialistResults(data)).not.toHaveProperty('secret')
  })
  it('only reads with session credentials, no-store, and validates the requested ID', async () => {
    const data = savedFixture()
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(data)))
    vi.stubGlobal('fetch', fetch)
    await readSpecialistResults(data.analysis_id)
    expect(fetch).toHaveBeenCalledWith(`/api/v1/analyses/${data.analysis_id}/results`, expect.objectContaining({
      method: 'GET', credentials: 'include', cache: 'no-store',
    }))
    fetch.mockResolvedValue(new Response(JSON.stringify(data)))
    await expect(readSpecialistResults('11111111-1111-4111-8111-111111111111')).rejects.toThrow()
  })
  it.each([[401, 'invalid_session'], [404, 'analysis_not_found']])('maps HTTP %s', async (status, code) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code } }), { status: Number(status) })))
    await expect(readSpecialistResults(savedFixture().analysis_id)).rejects.toMatchObject({ code })
  })
})
