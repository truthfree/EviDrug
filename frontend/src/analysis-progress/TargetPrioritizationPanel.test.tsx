import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import fixture from './fixtures/cdk4-api-summary.json'
import { parseTargetPrioritization, type TargetPrioritization } from './targetPrioritization'
import { TargetPrioritizationPanel } from './TargetPrioritizationPanel'

/** breast cancer UI 경계 테스트용 합성 대안이며 실제 생물학적 평가 결과가 아니다. */
function example(): TargetPrioritization {
  const result = parseTargetPrioritization(fixture)!
  result.alternatives = [
    {
      ...structuredClone(result.primary),
      rank: 2,
      ensembl_id: 'ENSG00000121879',
      approved_symbol: 'PIK3CA',
      uniprot_accession: 'P42336',
      eligibility: 'exploratory',
      eligibility_reason_codes: ['small_molecule_tractability_evidence_missing'],
      tractability_assessments: [],
      pharos_evidence: null,
      modulation_action: 'unknown',
      prioritization_rationale: '합성 fixture: 추가 검증이 필요한 후보입니다.',
      causal_rationale: '합성 fixture: 방향이 충돌하여 판단을 보류합니다.',
      causal_support: {
        status: 'supported',
        reason_codes: ['therapeutic_direction_conflicting'],
        therapeutic_direction: 'unknown',
        biological_evidence_count: 2,
        clinical_validation_count: 0,
        evidence: [
          {
            evidence_id: 'test-evidence-1',
            datasource_id: 'test-provider',
            datatype_id: 'somatic_mutation',
            axis: 'somatic',
            score: 0.5,
            disease_id: 'MONDO_0007254',
            disease_name: 'breast cancer',
            disease_scope: 'direct',
            direction_on_target: 'loss_of_function',
            direction_on_trait: 'risk',
            target_role: null,
            confidence: null,
            significant_driver_methods: [],
          },
        ],
      },
      causal_evidence_ids: ['test-evidence-1'],
    },
  ]
  result.excluded_candidates = [
    {
      ensembl_id: 'ENSG00000091831',
      approved_symbol: 'ESR1',
      association_score: 0.5,
      reason_code: 'not_shortlisted',
    },
  ]
  return result
}

describe('TargetPrioritizationPanel', () => {
  afterEach(cleanup)
  it('separates primary, exploratory alternatives, and excluded candidates', () => {
    render(<TargetPrioritizationPanel result={example()} />)
    expect(screen.getByRole('heading', { name: 'CDK4' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'PIK3CA' })).toBeInTheDocument()
    expect(screen.getByText('우선 추천 · 1순위')).toBeInTheDocument()
    expect(screen.getByText('탐색 후보 · 추가 검증 필요')).toBeInTheDocument()
    fireEvent.click(screen.getByText('제외된 후보 (1)'))
    expect(screen.getByText('이번 추천 목록에 포함되지 않음')).toBeVisible()
    expect(screen.getByText('not_shortlisted')).toBeVisible()
  })
  it('does not present association or tractability as clinical success probabilities', () => {
    render(<TargetPrioritizationPanel result={parseTargetPrioritization(fixture)} />)
    expect(screen.getByText(/성공 확률이나 약물 접근성 점수가 아닙니다/)).toBeInTheDocument()
    expect(
      screen.getByText(/입력 약물의 해당 질환 치료 승인을 뜻하지 않습니다/),
    ).toBeInTheDocument()
    expect(screen.getByText('0.631')).toBeInTheDocument()
    expect(screen.queryByText('63.1%')).not.toBeInTheDocument()
    expect(screen.getByText('승인 약물')).toBeInTheDocument()
    expect(screen.queryByText('후기 임상')).not.toBeInTheDocument()
    expect(screen.getByText('이번 실행에는 대안 후보가 없습니다.')).toBeInTheDocument()
  })
  it('shows Pharos maturity separately from causal and tractability evidence', () => {
    render(<TargetPrioritizationPanel result={parseTargetPrioritization(fixture)} />)
    const candidate = screen.getByRole('article', { name: 'CDK4' })
    const maturity = within(candidate).getByRole('region', { name: 'Pharos 표적 성숙도' })
    expect(within(maturity).getAllByText('Tclin')).toHaveLength(2)
    expect(within(maturity).getByText('임상 표적 · Kinase')).toBeInTheDocument()
    expect(within(maturity).getByText('승인된 의약품이 알려진 표적입니다.')).toBeInTheDocument()
    expect(within(maturity).getByText('526개')).toBeInTheDocument()
    expect(within(maturity).getByText('508건')).toBeInTheDocument()
    expect(within(maturity).getByText(/표적과 연결한 ligand 레코드 수/)).toBeInTheDocument()
    expect(within(maturity).getByText(/표적과 연결한 문헌 수/)).toBeInTheDocument()
    expect(within(maturity).getByText(/값이 높을수록 문헌 언급이 적어/)).toBeInTheDocument()
    expect(within(maturity).getByText(/표적 단백질 계열/)).toBeInTheDocument()
    expect(within(maturity).getByText(/질환 인과성이나 임상 성공 확률을/)).toBeInTheDocument()
    expect(screen.getByText(/Open Targets · Pharos/)).toBeInTheDocument()
    fireEvent.click(within(maturity).getByText('TDL 등급 기준 보기'))
    expect(within(maturity).getByText(/^Tdark$/)).toBeVisible()
    expect(within(maturity).getByText(/상대적으로 부족한 저연구 표적/)).toBeVisible()
  })
  it('keeps the existing card when Pharos evidence is absent', () => {
    const result = parseTargetPrioritization(fixture)!
    result.primary.pharos_evidence = null
    render(<TargetPrioritizationPanel result={result} />)
    expect(screen.queryByRole('region', { name: 'Pharos 표적 성숙도' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'CDK4' })).toBeInTheDocument()
  })
  it('shows direction conflict independently from supported causal status and explains provenance', () => {
    render(<TargetPrioritizationPanel result={example()} />)
    const candidate = screen.getByRole('article', { name: 'PIK3CA' })
    expect(within(candidate).getByText('인과 근거 지지')).toBeInTheDocument()
    expect(within(candidate).getByText(/치료 방향 근거 충돌:/)).toBeInTheDocument()
    fireEvent.click(within(candidate).getByText('정책 사유와 개별 근거 보기'))
    expect(within(candidate).getByText('기능 감소 (LoF) · 질환 위험 증가')).toBeVisible()
    expect(within(candidate).getByText('LLM이 인용한 근거')).toBeVisible()
    expect(within(candidate).getByText('test-evidence-1')).toBeVisible()
    expect(within(candidate).getByText(/인과성 확정이 아닙니다/)).toBeVisible()
  })
  it('handles absent summary and does not manufacture candidates', () => {
    render(<TargetPrioritizationPanel result={null} />)
    expect(screen.getByText(/표시할 Target 요약이 아직 없습니다/)).toBeInTheDocument()
    expect(screen.queryByRole('article')).not.toBeInTheDocument()
  })
  it('keeps long labels and unknown exclusion codes as text without injecting markup', () => {
    const result = example()
    result.primary.approved_symbol = 'LONG_TARGET_'.repeat(9)
    result.primary.prioritization_rationale = '<script>alert("not executable")</script>'
    result.excluded_candidates[0].reason_code = 'new_server_reason'
    const { container } = render(<TargetPrioritizationPanel result={result} />)
    expect(
      screen.getByRole('heading', { name: result.primary.approved_symbol }),
    ).toBeInTheDocument()
    expect(screen.getByText('추가 사유: new_server_reason')).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
  })
})
