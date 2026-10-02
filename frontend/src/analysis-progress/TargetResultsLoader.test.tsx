import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TargetResultsLoader } from './TargetResultsLoader'
import targetFixture from './fixtures/cdk4-api-summary.json'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('loads saved Target evidence with GET even when downstream stages failed', async () => {
  const analysisId = '11111111-1111-4111-8111-111111111111'
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    analysis_id: analysisId,
    status: 'failed',
    input: {
      disease_id: 'MONDO_0007254', disease_name: 'breast cancer',
      target_mode: 'specified', target_name: 'CDK4',
      original_smiles: 'CCO', canonical_smiles: 'CCO',
    },
    stages: ['target_hypothesis', 'admet', 'dta', 'decision'].map((name, index) => ({
      name, position: index + 1, status: index === 0 ? 'completed' : 'failed',
      updated_at: '2026-09-21T03:00:00Z',
    })),
    events: [], error_code: null, target_prioritization: targetFixture,
    created_at: '2026-09-21T03:00:00Z', updated_at: '2026-09-21T03:00:00Z',
  })))
  vi.stubGlobal('fetch', fetchMock)

  render(<TargetResultsLoader analysisId={analysisId} />)

  expect(await screen.findByRole('heading', { name: '타깃 후보와 근거' })).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'CDK4' })).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledWith(`/api/v1/analyses/${analysisId}`,
    expect.objectContaining({ method: 'GET', credentials: 'include' }))
})
