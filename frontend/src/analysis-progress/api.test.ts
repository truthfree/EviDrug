import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AnalysisInputResponse } from '../analysis-input/api'
import { createAnalysis, readAnalysis } from './api'
import targetFixture from './fixtures/cdk4-api-summary.json'

const input: AnalysisInputResponse = {
  disease_id: 'MONDO_0004975',
  disease_name: 'Alzheimer disease',
  target_mode: 'specified',
  target_name: 'BACE1',
  original_smiles: 'C(C)O',
  canonical_smiles: 'CCO',
  potency_criterion: { endpoint: 'Kd', maximum_value: 50, unit: 'pM' },
}

describe('analysis API', () => {
  it('returns the validated target summary even when downstream analysis failed', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse({ ...analysisResponse('failed'), target_prioritization: targetFixture }),
        ),
    )
    const result = await readAnalysis('saved-analysis')
    expect(result.status).toBe('failed')
    expect(result.target_prioritization).toEqual(targetFixture)
  })
  it('rejects malformed target summaries using the normal API error contract', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse({ ...analysisResponse('completed'), target_prioritization: {} }),
        ),
    )
    await expect(readAnalysis('saved-analysis')).rejects.toMatchObject({
      code: 'unexpected_response',
    })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('creates an analysis with the validated input and idempotency key', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(analysisResponse('queued')))
    vi.stubGlobal('fetch', fetchMock)

    const analysis = await createAnalysis(input, 'frontend:test-key')

    expect(analysis.status).toBe('queued')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/analyses',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        headers: expect.objectContaining({ 'Idempotency-Key': 'frontend:test-key' }),
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
  })

  it('reads the persisted state using an encoded analysis id', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(analysisResponse('running')))
    vi.stubGlobal('fetch', fetchMock)

    const analysis = await readAnalysis('analysis/id')

    expect(analysis.status).toBe('running')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/analyses/analysis%2Fid',
      expect.objectContaining({ method: 'GET', credentials: 'include' }),
    )
  })

  it('rejects a successful response that does not match the contract', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ status: 'queued' })))

    await expect(readAnalysis('analysis-id')).rejects.toMatchObject({
      code: 'unexpected_response',
    })
  })

  it('preserves the retry delay when analysis creation is rate limited', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse(
            { detail: { code: 'api_rate_limited', message: 'Request limit reached.' } },
            429,
            { 'Retry-After': '120' },
          ),
        ),
    )

    await expect(createAnalysis(input, 'frontend:test-key')).rejects.toMatchObject({
      code: 'rate_limited',
      retryAfterSeconds: 120,
    })
  })
})

export function analysisResponse(status: 'queued' | 'running' | 'completed' | 'failed') {
  const stageStatus =
    status === 'completed' ? 'completed' : status === 'failed' ? 'failed' : 'pending'
  return {
    analysis_id: '11111111-1111-4111-8111-111111111111',
    status,
    input,
    stages: ['target_hypothesis', 'admet', 'dta', 'decision'].map((name, index) => ({
      name,
      status: stageStatus,
      position: index + 1,
      updated_at: '2026-09-21T03:00:00Z',
    })),
    events: [
      {
        event_type: status === 'completed' ? 'analysis_completed' : 'analysis_created',
        status,
        reason_code: null,
        created_at: '2026-09-21T03:00:00Z',
      },
    ],
    error_code: status === 'failed' ? 'agent_not_configured' : null,
    created_at: '2026-09-21T03:00:00Z',
    updated_at: '2026-09-21T03:00:00Z',
  }
}

function jsonResponse(body: unknown, status = 200, headers?: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}
