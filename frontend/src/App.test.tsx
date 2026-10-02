import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const sessionResponse = {
  authenticated: true,
  expires_at: '2026-09-14T22:00:00Z',
}

describe('App authentication flow', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('keeps the public service explanation visible when there is no session', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 401 })))

    render(<App />)

    expect(
      await screen.findByRole('heading', { name: /흩어진 근거를 연결해/ }),
    ).toBeInTheDocument()
    expect(screen.getByText('접근 코드 필요')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '분석 시작' })).toBeEnabled()
    expect(screen.getByLabelText(/^앱 버전 v/)).toBeInTheDocument()
  })

  it('opens the access code panel and creates an authenticated session', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(jsonResponse(sessionResponse))
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '분석 시작' }))
    const accessCodeInput = screen.getByLabelText('접근 코드')
    fireEvent.change(accessCodeInput, { target: { value: 'shared-research-code' } })
    fireEvent.click(screen.getByRole('button', { name: '계속' }))

    expect(
      await screen.findByRole('heading', { name: '새로운 연구 질문을 시작하세요.' }),
    ).toBeInTheDocument()
    expect(screen.getByText('안전하게 연결됨')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/v1/auth/session',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({ access_code: 'shared-research-code' }),
      }),
    )
  })

  it('explains an invalid access code without clearing the input', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(new Response(null, { status: 401 }))
        .mockResolvedValueOnce(
          jsonResponse(
            {
              detail: {
                code: 'invalid_access_code',
                message: 'The access code is invalid.',
              },
            },
            401,
          ),
        ),
    )

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '분석 시작' }))
    const accessCodeInput = screen.getByLabelText('접근 코드')
    fireEvent.change(accessCodeInput, { target: { value: 'incorrect-code' } })
    fireEvent.click(screen.getByRole('button', { name: '계속' }))

    expect(
      await screen.findByText('접근 코드가 올바르지 않습니다. 전달받은 코드를 다시 확인해 주세요.'),
    ).toBeInTheDocument()
    expect(accessCodeInput).toHaveValue('incorrect-code')
  })

  it('shows when the attempt limit can be retried', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(new Response(null, { status: 401 }))
        .mockResolvedValueOnce(
          jsonResponse(
            {
              detail: {
                code: 'too_many_auth_attempts',
                message: 'Too many authentication attempts.',
              },
            },
            429,
            { 'Retry-After': '120' },
          ),
        ),
    )

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '분석 시작' }))
    fireEvent.change(screen.getByLabelText('접근 코드'), { target: { value: 'retry-code' } })
    fireEvent.click(screen.getByRole('button', { name: '계속' }))

    expect(
      await screen.findByText('인증 시도가 잠시 제한되었습니다. 약 2분 뒤 다시 시도해 주세요.'),
    ).toBeInTheDocument()
  })

  it('restores an existing authenticated session on page load', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(sessionResponse)))

    render(<App />)

    expect(
      await screen.findByRole('heading', { name: '새로운 연구 질문을 시작하세요.' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '로그아웃' })).toBeInTheDocument()
  })

  it('returns to the public landing after logout', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(sessionResponse))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '로그아웃' }))

    expect(
      await screen.findByRole('heading', { name: /흩어진 근거를 연결해/ }),
    ).toBeInTheDocument()
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/v1/auth/session',
      expect.objectContaining({ method: 'DELETE', credentials: 'include' }),
    )
  })

  it('keeps public information available and retries a failed session check', async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(
      await screen.findByText(
        '인증 서버에 연결할 수 없습니다. 공개 안내는 계속 확인할 수 있습니다.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /흩어진 근거를 연결해/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '다시 연결' }))

    expect(await screen.findByText('접근 코드 필요')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('searches disease candidates and requires an explicit selection', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(sessionResponse))
      .mockResolvedValueOnce(
        jsonResponse({
          query: 'Alzheimer',
          candidates: [
            {
              id: 'MONDO_0004975',
              name: 'Alzheimer disease',
              description: 'A progressive neurodegenerative disease.',
              relevance_score: 5666.6562,
            },
          ],
          requires_confirmation: true,
          source: 'open_targets',
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    const continueButton = await screen.findByRole('button', {
      name: '선택한 질환으로 계속',
    })
    expect(continueButton).toBeDisabled()

    fireEvent.change(screen.getByRole('combobox', { name: 'Disease or phenotype' }), {
      target: { value: 'Alzheimer' },
    })

    expect(await screen.findByText('이 질환을 찾으신 건가요?')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('option', { name: /Alzheimer disease/ }))

    expect(continueButton).toBeEnabled()
    expect(screen.getByText('MONDO_0004975')).toBeInTheDocument()
    fireEvent.click(continueButton)

    expect(
      screen.getByText(/Alzheimer disease을 분석 질환으로 확인했습니다/),
    ).toBeInTheDocument()
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/v1/diseases/search',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({ query: 'Alzheimer' }),
      }),
    )
  })

  it('guides the user when disease search has no matching candidates', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(sessionResponse))
        .mockResolvedValueOnce(
          jsonResponse(
            {
              detail: {
                code: 'disease_candidates_not_found',
                message: 'No matching disease or phenotype was found.',
              },
            },
            404,
          ),
        ),
    )

    render(<App />)

    fireEvent.change(
      await screen.findByRole('combobox', { name: 'Disease or phenotype' }),
      { target: { value: 'alzhemier disease' } },
    )

    expect(
      await screen.findByText(
        '일치하는 질환을 찾지 못했습니다. 철자나 더 짧은 표현을 확인해 주세요.',
      ),
    ).toBeInTheDocument()
  })

  it('validates target mode and SMILES after disease confirmation', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(sessionResponse))
      .mockResolvedValueOnce(
        jsonResponse({
          query: 'Alzheimer',
          candidates: [
            {
              id: 'MONDO_0004975',
              name: 'Alzheimer disease',
              description: 'A progressive neurodegenerative disease.',
              relevance_score: 5666.6562,
            },
          ],
          requires_confirmation: true,
          source: 'open_targets',
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          disease_id: 'MONDO_0004975',
          disease_name: 'Alzheimer disease',
          target_mode: 'specified',
          target_name: 'BACE1',
          original_smiles: 'C(C)O',
          canonical_smiles: 'CCO',
          potency_criterion: { endpoint: 'Kd', maximum_value: 100, unit: 'nM' },
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    fireEvent.change(
      await screen.findByRole('combobox', { name: 'Disease or phenotype' }),
      { target: { value: 'Alzheimer' } },
    )
    fireEvent.click(await screen.findByRole('option', { name: /Alzheimer disease/ }))
    fireEvent.click(screen.getByRole('button', { name: '선택한 질환으로 계속' }))

    expect(
      screen.getByRole('heading', { name: '어떤 조건으로 화합물을 살펴볼까요?' }),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: /특정 타깃 평가/ }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Target name' }), {
      target: { value: 'BACE1' },
    })
    fireEvent.change(screen.getByRole('textbox', { name: 'SMILES' }), {
      target: { value: 'C(C)O' },
    })
    expect(screen.getByText('DTA 고정 기준 · Kd ≤ 100 nM (pKd ≥ 7.0)')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '분석 입력 확인' }))

    const canonicalLabel = await screen.findByText('Canonical SMILES')
    expect(canonicalLabel).toBeInTheDocument()
    expect(canonicalLabel.nextElementSibling).toHaveTextContent('CCO')
    expect(
      screen.getByRole('heading', { name: '확인한 입력으로 실제 분석 작업을 시작합니다.' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '실제 분석 실행' })).toBeEnabled()
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      '/api/v1/analysis-inputs/validate',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({
          disease_id: 'MONDO_0004975',
          disease_name: 'Alzheimer disease',
          target_mode: 'specified',
          target_name: 'BACE1',
          smiles: 'C(C)O',
          potency_criterion: { endpoint: 'Kd', maximum_value: 100, unit: 'nM' },
        }),
      }),
    )

    fireEvent.change(screen.getByRole('textbox', { name: 'SMILES' }), {
      target: { value: 'CCN' },
    })
    expect(screen.queryByText('Canonical SMILES')).not.toBeInTheDocument()
  })

  it('explains an invalid SMILES in target discovery mode', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(sessionResponse))
        .mockResolvedValueOnce(
          jsonResponse({
            query: 'Alzheimer',
            candidates: [
              {
                id: 'MONDO_0004975',
                name: 'Alzheimer disease',
                description: 'A progressive neurodegenerative disease.',
                relevance_score: 5666.6562,
              },
            ],
            requires_confirmation: true,
            source: 'open_targets',
          }),
        )
        .mockResolvedValueOnce(
          jsonResponse(
            {
              detail: {
                code: 'invalid_smiles',
                message: 'The SMILES input is not a valid molecule.',
              },
            },
            422,
          ),
        ),
    )

    render(<App />)

    fireEvent.change(
      await screen.findByRole('combobox', { name: 'Disease or phenotype' }),
      { target: { value: 'Alzheimer' } },
    )
    fireEvent.click(await screen.findByRole('option', { name: /Alzheimer disease/ }))
    fireEvent.click(screen.getByRole('button', { name: '선택한 질환으로 계속' }))
    fireEvent.click(screen.getByRole('radio', { name: /타깃 없이 탐색/ }))

    expect(screen.queryByRole('textbox', { name: 'Target name' })).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: 'SMILES' }), {
      target: { value: 'not-a-smiles' },
    })
    fireEvent.click(screen.getByRole('button', { name: '분석 입력 확인' }))

    expect(
      await screen.findByText(
        '분자 구조로 해석할 수 없는 SMILES입니다. 괄호, 결합과 원자가 표현을 확인해 주세요.',
      ),
    ).toBeInTheDocument()
  })
})

function jsonResponse(
  body: unknown,
  status = 200,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}
