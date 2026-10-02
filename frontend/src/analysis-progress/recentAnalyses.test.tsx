import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SavedResultsLookup } from './SavedResultsLookup'

const analysisId = '11111111-1111-4111-8111-111111111111'
const recent = {
  analysis_id: analysisId,
  status: 'completed',
  disease_name: 'breast cancer',
  target_mode: 'specified',
  target_name: 'CDK4',
  canonical_smiles: 'CCO',
  created_at: '2026-09-28T03:00:00Z',
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('saved results recent list', () => {
  it('shows metadata only until the user chooses a result', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [recent] })))
    const onSelectAnalysis = vi.fn()
    vi.stubGlobal('fetch', fetch)
    render(<SavedResultsLookup onSelectAnalysis={onSelectAnalysis} />)

    expect(await screen.findByText('breast cancer')).toBeInTheDocument()
    expect(screen.getByText('지정 표적 · CDK4')).toBeInTheDocument()
    expect(screen.getByText('CCO')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '분석 결과' })).not.toBeInTheDocument()
    expect(fetch).toHaveBeenCalledOnce()
    expect(fetch.mock.calls[0][1].credentials).toBe('include')

    fireEvent.click(screen.getByRole('button', { name: '결과 보기' }))
    expect(onSelectAnalysis).toHaveBeenCalledWith(analysisId)
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('handles invalid responses and retries the list independently', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ ...recent, canonical_smiles: null }] })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [recent] })))
    vi.stubGlobal('fetch', fetch)
    render(<SavedResultsLookup onSelectAnalysis={vi.fn()} />)

    expect(await screen.findByRole('alert')).toHaveTextContent('최근 기록을 불러오지 못했습니다')
    fireEvent.click(screen.getByRole('button', { name: '목록 새로고침' }))
    expect(await screen.findByText('breast cancer')).toBeInTheDocument()
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
  })

  it('does not load details when the list is empty', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] })))
    vi.stubGlobal('fetch', fetch)
    render(<SavedResultsLookup onSelectAnalysis={vi.fn()} />)
    expect(await screen.findByText(/이 브라우저에 연결된 최근 분석이 없습니다/)).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledOnce()
  })
})
