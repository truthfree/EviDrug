import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { SpecialistResultsPanel } from './SpecialistResultsPanel'
import { structuredFixture } from './decisionV57TestFixture'
import { parseSpecialistResults } from './specialistResults'
import { buildSpecialistResultsReview } from './specialistResultsReview'
import serverProjections from './fixtures/decision-v57-server-projection.json'

afterEach(cleanup)
describe('v5.7 structured Decision', () => {
  it.each([0, 1])('accepts current backend public projection %s through rendering and export', (index) => {
    const { data, analysis } = structuredFixture(index)
    const publicDecision = serverProjections[index]
    const parsed = parseSpecialistResults({ ...data,
      decision: { ...data.decision, result: publicDecision } })
    render(<SpecialistResultsPanel data={parsed} analysis={analysis} />)
    const decision = screen.getByRole('region', { name: 'Decision 저장 결과' })
    expect(within(decision).getByText(publicDecision.headline!)).toBeInTheDocument()
    expect(parsed.decision.result?.context_version).toBe('decision-context-v5.7')
    expect(parsed.decision.result?.server_metadata).toEqual(publicDecision.server_metadata)
    const review = buildSpecialistResultsReview(parsed, analysis)
    expect(review.agent_results.decision?.result.assessment).toEqual(publicDecision.assessment)
    expect(review.agent_results.decision?.result.server_metadata).toEqual(publicDecision.server_metadata)
    expect(review.agent_results.decision?.result.next_actions).toEqual(publicDecision.next_actions)
  })

  it.each([0, 1])('renders attached case %s with four assessments and folded rationale', (index) => {
    const { data, analysis } = structuredFixture(index)
    const { container } = render(<SpecialistResultsPanel data={data} analysis={analysis} />)
    const decision = screen.getByRole('region', { name: 'Decision 저장 결과' })
    for (const area of ['표적–질환', '결합 예측', 'ADME', '안전성']) {
      expect(within(decision).getByRole('article', { name: `${area} 평가` })).toBeInTheDocument()
    }
    expect(within(decision).getByText(data.decision.result!.headline!)).toBeInTheDocument()
    const rationale = within(decision).getByText(data.decision.result!.rationale).closest('details')
    expect(rationale).not.toHaveAttribute('open')
    expect(within(decision).getByRole('heading', { name: 'Conditional Go 조건부 추가 연구' })).toBeInTheDocument()
    expect(within(decision).queryByRole('region', { name: '상충하는 근거' })).toBeNull()
    expect(within(decision).getByRole('region', { name: '핵심 강점' })).toBeInTheDocument()
    expect(container.textContent).not.toContain('same_region_below_reference')
    const review = buildSpecialistResultsReview(data, analysis)
    expect(review.agent_results.decision?.result.server_metadata?.lead_candidate?.symbol).toBe(index === 0 ? 'CDK4' : 'BRCA1')
    expect(review.agent_results.admet?.result.toxicity_axes).toEqual(data.admet.result!.toxicity_axes)
    expect(review.agent_results.decision?.result.decision_phase).toBe('final')
    expect(review.agent_results.decision?.result.recall_request).toBeNull()
  })

  it('exports a readable policy mismatch reason even when Decision has no result', () => {
    const { data, analysis } = structuredFixture()
    data.decision.result = null
    data.decision.error_code = 'decision_dta_assay_policy_mismatch'
    expect(buildSpecialistResultsReview(data, analysis).execution_errors[0].message).toContain('DTA부터 다시 실행')
  })

  it('uses the server lead and original candidate evidence', () => {
    const { data, analysis } = structuredFixture(1)
    render(<SpecialistResultsPanel data={data} analysis={analysis} />)
    expect(screen.getAllByText('대표 후보: BRCA1')).toHaveLength(2)
    const table = screen.getByRole('table')
    const row = within(table).getByRole('row', { name: /BRCA1 · 대표 후보/ })
    expect(row).toHaveClass('specialist-results__lead-row')
    expect(row).toHaveTextContent('지지됨 · 활성화')
    expect(row).toHaveTextContent('두 모델 모두 기준선 미만')
  })

  it('preserves shared axis badges and raw channel labels without requiring LLM key values', () => {
    const { data, analysis } = structuredFixture()
    data.calls = parseSpecialistResults({ ...data, calls: [{ agent_name: 'admet', call_number: 2,
      run_id: data.analysis_id, status: 'completed', purpose: 'evidence_followup',
      triggering_run_id: null, responds_to_run_id: null, response_run_id: null, request: null,
      result_kind: 'cardiac_ion_channel', projection_status: 'available', error_code: null,
      result: { source_tool_call_id: data.analysis_id, tool_call_id: data.analysis_id,
        tool_version: 'fixture', execution_mode: 'live', limitations: [],
        predictions: [{ channel: 'herg', label: 'negative', class_probability: 0.9 }] } }] }).calls
    render(<SpecialistResultsPanel data={data} analysis={analysis} />)
    const safety = screen.getByRole('article', { name: '안전성 평가' })
    const admet = screen.getByRole('region', { name: 'ADMET 저장 결과' })
    for (const region of [safety, admet]) {
      const priorities = within(region).getByRole('region', { name: '독성 확인시험 우선순위' })
      expect(within(priorities).getByText(/hERG ·/)).toHaveTextContent('현재 우선 확인 대상 아님')
      expect(within(region).getByText(/CToxPred2 · hERG · 비차단 예측/)).toHaveTextContent('비차단 분류 확률 0.9')
    }
  })

  it('rejects malformed structured areas and values', () => {
    const { data } = structuredFixture()
    const bad = structuredClone(data)
    bad.decision.result!.assessment!.dta.key_values[0].value = NaN
    expect(() => parseSpecialistResults(bad)).toThrow()
    expect(() => parseSpecialistResults({ ...data, decision: { ...data.decision,
      result: { ...data.decision.result, assessment: { target: {} } } } })).toThrow()
  })
})
