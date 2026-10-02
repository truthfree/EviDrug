import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SpecialistResultsPanel } from './SpecialistResultsPanel'
import { partialFixture, savedFixture } from './specialistTestFixture'
import { parseSpecialistResults } from './specialistResults'
import { buildSpecialistResultsReview } from './specialistResultsReview'
import type { Analysis } from './api'

afterEach(cleanup)
describe('specialist cards', () => {
  it('downloads only input context and agent feedback fields as formatted JSON', async () => {
    const data = savedFixture()
    const analysis = analysisResponse('completed')
    const review = buildSpecialistResultsReview(data, analysis)
    const serialized = JSON.stringify(review)
    expect(review.input_context).toMatchObject({ disease_name: 'Alzheimer disease', canonical_smiles: 'CCO' })
    expect(serialized).not.toContain('run_id')
    expect(serialized).not.toContain('tool_call_id')
    expect(serialized).not.toContain('token_usage')
    expect(serialized).not.toContain('duration_ms')
    expect(review.agent_results.admet?.result.endpoints[0]).toMatchObject({
      value: 0.2732613801956177,
      display_value: '0.27',
      display_percentile: '63.16',
    })

    const createObjectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:results')
    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    render(<SpecialistResultsPanel data={data} analysis={analysis} />)
    fireEvent.click(screen.getByRole('button', { name: '결과 JSON 다운로드' }))
    expect(createObjectURL).toHaveBeenCalledOnce()
    expect(click).toHaveBeenCalledOnce()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:results')
    createObjectURL.mockRestore()
    revokeObjectURL.mockRestore()
    click.mockRestore()
  })
  it('separates selected ADMET results from failed downstream runs', () => {
    renderPanel(savedFixture())
    expect(screen.getByText(/전체 49개 중 12개/)).toHaveTextContent('실행 실패를 의미하지 않습니다')
    expect(screen.getByText('dta_candidates_incomplete')).toBeInTheDocument()
    expect(screen.getAllByText(/DrugBank 승인 약물 기준 백분위/, { selector: 'dt' })).toHaveLength(12)
  })
  it('keeps DTA values when only reasoning failed and limits decision to research', () => {
    const data = partialFixture()
    data.decision.result!.used_evidence_ids = ['admet:context', 'dta:ENSG00000135446']
    const review = buildSpecialistResultsReview(data, analysisResponse('partial_failure'))
    expect(review.agent_results.dta?.result.candidates[0].predictions[0].observations[0])
      .toMatchObject({ value: 4.811026573181152, display_value: '4.81' })
    expect(review.agent_results.decision?.result.next_actions).toEqual(data.decision.result!.next_actions)
    expect(review.agent_results.decision?.evidence_manifest).toEqual([
      { evidence_id: 'admet:context', status: 'available', export_path: 'agent_results.admet' },
      { evidence_id: 'dta:ENSG00000135446', status: 'available',
        export_path: 'agent_results.dta.result.candidates[ensembl_id=ENSG00000135446]' },
    ])
    expect(review.agent_results.decision?.evidence_audit)
      .toEqual({ status: 'complete', missing_evidence_ids: [] })
    renderPanel(data)
    expect(screen.getByRole('status')).toHaveTextContent('부분 완료')
    expect(screen.getByText(/predicted_pkd: 4.81/)).toBeInTheDocument()
    expect(screen.getByText(/예측값의 존재 여부와는 별개/)).toBeInTheDocument()
    expect(screen.getByText('조건부 추가 연구')).toBeInTheDocument()
    expect(screen.getByText(/치료 권고 또는 승인 여부를 뜻하지/)).toBeInTheDocument()
    const actions = screen.getByRole('region', { name: '다음 연구 행동' })
    expect(within(actions).getByText('제안')).toBeInTheDocument()
    expect(within(actions).getByText(/아직 수행되지 않은 제안/)).toBeInTheDocument()
    expect(within(actions).getByText(/정량 결합 실험/)).toBeInTheDocument()
    expect(within(actions).getByText('제안 이유')).toBeInTheDocument()
    expect(within(actions).getByText('판정에 미치는 영향')).toBeInTheDocument()
    expect(within(actions).getByText(/Agent 호출 흐름에서 별도로/)).toBeInTheDocument()
    const dta = screen.getByRole('region', { name: 'DTA 저장 결과' })
    expect(within(dta).getByText(/이번 조회 비용 아님/)).toHaveTextContent('정보 없음')
  })
  it('marks unknown Decision evidence as audit incomplete instead of hiding it', () => {
    const data = partialFixture()
    const review = buildSpecialistResultsReview(data, analysisResponse('partial_failure'))
    expect(review.agent_results.decision?.evidence_audit).toEqual({
      status: 'audit_incomplete',
      missing_evidence_ids: ['test-evidence'],
    })
    expect(review.agent_results.decision?.evidence_manifest).toEqual([{
      evidence_id: 'test-evidence', status: 'missing', export_path: null,
    }])
  })
  it('keeps older Decision results readable when structured actions are absent', () => {
    const data = partialFixture()
    const legacy = structuredClone(data) as unknown as Record<string, unknown>
    const decision = (legacy.decision as Record<string, unknown>).result as Record<string, unknown>
    delete decision.next_actions
    renderPanel(parseSpecialistResults(legacy))
    const actions = screen.getByRole('region', { name: '다음 연구 행동' })
    expect(within(actions).getByText(/구조화된 다음 행동이 제공되지 않았습니다/)).toBeInTheDocument()
    expect(screen.getByText('조건부 추가 연구')).toBeInTheDocument()
  })
  it('shows two DTA model predictions with separate tool call IDs', () => {
    const data = partialFixture()
    const candidate = data.dta.result!.candidates[0]
    const first = '77fba38e-1cd6-5eba-9286-6842cc0bf18d'
    const second = 'd26de727-ea4c-5e4b-9391-f413420fd609'
    candidate.tool_call_id = first
    candidate.model_runs = [
      { tool_id: 'dta', tool_call_id: first, status: 'succeeded', model: candidate.model,
        observations: candidate.observations, error_code: null, duration_ms: 100 },
      { tool_id: 'dta_mpnn_cnn_bindingdb', tool_call_id: second, status: 'succeeded',
        model: { provider: 'test', model_id: 'MPNN_CNN_BindingDB', version: 'test-v1' },
        observations: [{ score_type: 'predicted_pkd', value: 6.590145587921143, unit: '-log10(Kd [M])' }],
        error_code: null, duration_ms: 120 },
    ]
    renderPanel(data)
    const dta = screen.getByRole('region', { name: 'DTA 저장 결과' })
    expect(within(dta).getAllByText(/predicted_pkd: 4.81/)).toHaveLength(1)
    expect(within(dta).getByText(/predicted_pkd: 6.59/)).toBeInTheDocument()
    expect(within(dta).getByText(first)).toBeInTheDocument()
    expect(within(dta).getByText(second)).toBeInTheDocument()
  })
  it('keeps a failed second DTA model distinct from the successful first model', () => {
    const data = partialFixture()
    const candidate = data.dta.result!.candidates[0]
    candidate.model_runs = [
      { tool_id: 'dta', tool_call_id: candidate.tool_call_id, status: 'succeeded',
        model: candidate.model, observations: candidate.observations, error_code: null, duration_ms: 100 },
      { tool_id: 'dta_mpnn_cnn_bindingdb', tool_call_id: null, status: 'skipped',
        model: null, observations: [], error_code: 'tool_budget_exhausted', duration_ms: 0 },
    ]
    renderPanel(data)
    const dta = screen.getByRole('region', { name: 'DTA 저장 결과' })
    expect(within(dta).getByText(/predicted_pkd: 4.81/)).toBeInTheDocument()
    expect(within(dta).getByText(/tool_budget_exhausted/)).toBeInTheDocument()
    expect(within(dta).queryByText(/predicted_pkd: 0/)).not.toBeInTheDocument()
  })
  it('shows exact assay evidence, relationship, and conditional PubChem status', () => {
    const data = partialFixture()
    const candidate = data.dta.result!.candidates[0]
    candidate.experimental_evidence = [{
      source: 'bindingdb', source_record_id: 'monomer:42:Kd:50:123',
      kind: 'quantitative', endpoint: 'Kd', value: 50, unit: 'nM',
      qualitative_outcome: null, assay_description: 'direct binding assay',
      doi: '10.test/example', pmid: '123',
    }, {
      source: 'pubchem', source_record_id: 'aid:7:sid:8:Inactive',
      kind: 'qualitative', endpoint: null, value: null, unit: null,
      qualitative_outcome: 'Inactive', assay_description: null, doi: null, pmid: null,
    }]
    candidate.evidence_assessment = {
      region_status: null, experimental_binding_support: null, pubchem_execution_issue: null, criterion: null,
      status: 'PREDICTION_EXPERIMENT_CONFLICT', recall_trigger: 'prediction_experiment_conflict',
      pubchem_requested: true, pubchem_executed: true, pubchem_resolved: false,
      limitations: ['endpoint별로 별도 해석합니다.'],
    }
    candidate.evidence_errors = ['chembl:provider_timeout']
    const review = buildSpecialistResultsReview(data, analysisResponse('partial_failure'))
    expect(review.agent_results.dta?.result.candidates[0]).toMatchObject({
      experimental_evidence: [{
        source: 'bindingdb',
        source_record_id: 'monomer:42:Kd:50:123',
        endpoint: 'Kd',
        value: 50,
        display_value: '50',
        unit: 'nM',
        doi: '10.test/example',
        pmid: '123',
      }, {
        source: 'pubchem',
        source_record_id: 'aid:7:sid:8:Inactive',
        value: null,
        display_value: null,
      }],
      evidence_assessment: {
        status: 'PREDICTION_EXPERIMENT_CONFLICT',
        pubchem_requested: true,
        pubchem_executed: true,
        pubchem_resolved: false,
      },
      evidence_errors: ['chembl:provider_timeout'],
    })
    renderPanel(data)
    const evidence = screen.getByRole('region', { name: 'CDK4 실험근거' })
    expect(within(evidence).getByText('예측과 직접 비교 가능한 실험값이 충돌함')).toBeInTheDocument()
    expect(within(evidence).getByText('Kd 50 nM')).toBeInTheDocument()
    expect(within(evidence).getByText('정성 결과: Inactive')).toBeInTheDocument()
    expect(within(evidence).getByText('chembl:provider_timeout')).toBeInTheDocument()
    expect(within(evidence).getByText(/결합하지 않는다는 의미가 아닙니다/)).toBeInTheDocument()
    expect(within(evidence).getByText('호출 시도함')).toBeInTheDocument()
  })
  it('leads with the decision and shows numbered follow-up evidence', () => {
    const data = partialFixture()
    const id = data.analysis_id
    const withCalls = parseSpecialistResults({ ...data, calls: [{
      agent_name: 'admet', call_number: 2, run_id: id, status: 'completed', purpose: 'evidence_followup',
      triggering_run_id: id, responds_to_run_id: id, response_run_id: null,
      request: { requesting_run_id: id, gap_kind: 'cardiac', objective: '심장 독성 근거 확인',
        reason_code: 'cardiac_evidence_gap', required_endpoints: ['hERG'] },
      result_kind: 'cardiac_ion_channel', projection_status: 'available', error_code: null,
      result: { source_tool_call_id: id, tool_call_id: id, tool_version: 'test', execution_mode: 'live',
        predictions: [{ channel: 'herg', label: 'positive', class_probability: 0.81 }], limitations: [] },
    }] })
    withCalls.decision.result!.used_evidence_ids = ['admet:cardiac_ion_channels']
    const review = buildSpecialistResultsReview(withCalls, analysisResponse('partial_failure'))
    expect(review.agent_results.decision?.evidence_audit.status).toBe('complete')
    expect(review.agent_results.decision?.cardiac_followups[0].predictions[0])
      .toMatchObject({ channel: 'herg', class_probability: 0.81, display_probability: '0.81' })
    renderPanel(withCalls)
    const decision = screen.getByRole('region', { name: 'Decision 저장 결과' })
    const calls = screen.getByRole('region', { name: 'Agent 호출 흐름' })
    const admet = screen.getByRole('region', { name: 'ADMET 저장 결과' })
    const dta = screen.getByRole('region', { name: 'DTA 저장 결과' })
    expect(decision.compareDocumentPosition(dta) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(dta.compareDocumentPosition(admet) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(admet.compareDocumentPosition(calls) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(calls).getByText(/ADMET 2번/)).toBeInTheDocument()
    expect(within(calls).getByText(/심장 독성 근거 확인/)).toBeInTheDocument()
    expect(within(calls).getByText(/CToxPred2 · hERG · 차단 예측/)).toHaveTextContent('차단 분류 확률 0.81')
  })
  it.each(['invalid', 'unavailable'] as const)('explains %s projections', (projection) => {
    const data = savedFixture()
    data.admet.result = null
    data.admet.projection_status = projection
    data.admet.status = null
    renderPanel(data)
    expect(screen.getByText('실행 기록 없음')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'ADMET 저장 결과' })).not.toHaveTextContent('AMES')
  })
  it('escapes model text and never links unsafe source URLs', () => {
    const data = partialFixture()
    data.decision.result!.rationale = '<img src=x onerror=alert(1)>'
    data.decision.result!.next_actions[0].action = '<script>alert(2)</script>'
    data.admet.result!.endpoints[0].source_url = 'javascript:alert(1)'
    const { container } = renderPanel(data)
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText('<script>alert(2)</script>')).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull()
    expect(container.querySelector('details')?.open).toBe(false)
  })
  it.each([
    ['go', 'Go', '추가 연구 진행'],
    ['conditional_go', 'Conditional Go', '조건부 추가 연구'],
    ['no_go', 'No-Go', '추가 연구 보류'],
  ] as const)('labels %s as research prioritization', (verdict, title, label) => {
    const data = partialFixture()
    data.decision.result!.verdict = verdict
    renderPanel(data)
    const heading = screen.getByRole('heading', { name: `${title} ${label}` })
    expect(heading).toHaveClass(`specialist-results__verdict--${verdict}`)
  })
})

function renderPanel(data: ReturnType<typeof savedFixture>) {
  return render(<SpecialistResultsPanel data={data} analysis={analysisResponse('completed')} />)
}

function analysisResponse(status: Analysis['status']): Analysis {
  return {
    analysis_id: '11111111-1111-4111-8111-111111111111',
    status,
    input: {
      disease_id: 'MONDO_0004975',
      disease_name: 'Alzheimer disease',
      target_mode: 'specified',
      target_name: 'BACE1',
      original_smiles: 'C(C)O',
      canonical_smiles: 'CCO',
    },
    stages: [],
    events: [],
    target_prioritization: null,
    error_code: null,
    created_at: '2026-09-28T03:00:00Z',
    updated_at: '2026-09-28T03:00:00Z',
  }
}
