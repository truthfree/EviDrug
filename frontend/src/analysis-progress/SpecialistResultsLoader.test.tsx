import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { SpecialistResultsLoader } from './SpecialistResultsLoader'
import { SavedResultsLookup } from './SavedResultsLookup'
import { savedFixture } from './specialistTestFixture'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })
it('navigates by valid ID without loading result details in the list', async () => {
  const data = savedFixture()
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] })))
  const onSelectAnalysis = vi.fn()
  vi.stubGlobal('fetch', fetch)
  render(<SavedResultsLookup onSelectAnalysis={onSelectAnalysis} />)
  await screen.findByText(/이 브라우저에 연결된 최근 분석이 없습니다/)
  expect(fetch).toHaveBeenCalledOnce()
  fireEvent.change(screen.getByLabelText('분석 ID'), { target: { value: 'bad' } })
  fireEvent.click(screen.getByRole('button', { name: '저장 결과 조회' }))
  expect(screen.getByRole('alert')).toHaveTextContent('UUID')
  expect(fetch).toHaveBeenCalledOnce()
  expect(onSelectAnalysis).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('분석 ID'), { target: { value: data.analysis_id } })
  fireEvent.click(screen.getByRole('button', { name: '저장 결과 조회' }))
  expect(onSelectAnalysis).toHaveBeenCalledWith(data.analysis_id)
  expect(screen.queryByRole('heading', { name: '분석 결과' })).not.toBeInTheDocument()
  expect(fetch).toHaveBeenCalledOnce()
})
it('shows loading, auth errors and explicit GET retry without analysis creation', async () => {
  const data = savedFixture()
  let resultsRequest = 0
  const fetch = vi.fn().mockImplementation((url: string) => {
    if (url.endsWith('/results')) {
      resultsRequest += 1
      return Promise.resolve(resultsRequest === 1
        ? new Response(JSON.stringify({ detail: { code: 'invalid_session' } }), { status: 401 })
        : jsonResponse(data))
    }
    return Promise.resolve(jsonResponse(analysisResponse(data.analysis_id)))
  })
  vi.stubGlobal('fetch', fetch)
  render(<SpecialistResultsLoader analysisId={data.analysis_id} />)
  expect(screen.getByRole('status')).toBeInTheDocument()
  expect(await screen.findByRole('alert')).toHaveTextContent('인증이 필요')
  fireEvent.click(screen.getByRole('button', { name: '결과 조회 재시도' }))
  await screen.findByRole('heading', { name: '분석 결과' })
  expect(fetch).toHaveBeenCalledTimes(4)
  expect(fetch.mock.calls.map((call) => call[1].method)).toEqual(['GET', 'GET', 'GET', 'GET'])
})
it('aborts an old lookup and ignores late responses when the ID changes', async () => {
  const first = savedFixture(), second = savedFixture()
  second.analysis_id = '11111111-1111-4111-8111-111111111111'
  const resolvers: Array<(value: Response) => void> = []
  const fetch = vi.fn().mockImplementation((url: string) => {
    if (url.includes(first.analysis_id)) {
      return new Promise<Response>((done) => { resolvers.push(done) })
    }
    return Promise.resolve(url.endsWith('/results')
      ? jsonResponse(second)
      : jsonResponse(analysisResponse(second.analysis_id)))
  })
  vi.stubGlobal('fetch', fetch)
  const view = render(<SpecialistResultsLoader analysisId={first.analysis_id} />)
  view.rerender(<SpecialistResultsLoader analysisId={second.analysis_id} />)
  await screen.findByText(second.analysis_id)
  expect(fetch.mock.calls[0][1].signal.aborted).toBe(true)
  expect(fetch.mock.calls[1][1].signal.aborted).toBe(true)
  await act(async () => {
    resolvers[0](jsonResponse(first))
    resolvers[1](jsonResponse(analysisResponse(first.analysis_id)))
  })
  await waitFor(() => expect(screen.queryByText(first.analysis_id)).not.toBeInTheDocument())
})

function analysisResponse(analysisId: string) {
  return {
    analysis_id: analysisId,
    status: 'completed',
    input: {
      disease_id: 'MONDO_0004975', disease_name: 'Alzheimer disease', target_mode: 'specified',
      target_name: 'BACE1', original_smiles: 'C(C)O', canonical_smiles: 'CCO',
    },
    stages: ['target_hypothesis', 'admet', 'dta', 'decision'].map((name, index) => ({
      name, status: 'completed', position: index + 1, updated_at: '2026-09-21T03:00:00Z',
    })),
    events: [{ event_type: 'analysis_completed', status: 'completed', reason_code: null,
      created_at: '2026-09-21T03:00:00Z' }],
    target_prioritization: null,
    error_code: null,
    created_at: '2026-09-21T03:00:00Z',
    updated_at: '2026-09-21T03:00:00Z',
  }
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })
}
