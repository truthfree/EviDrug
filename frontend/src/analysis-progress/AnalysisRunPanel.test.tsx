import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AnalysisInputResponse } from '../analysis-input/api'
import { AnalysisRunPanel } from './AnalysisRunPanel'
import targetFixture from './fixtures/cdk4-api-summary.json'

const input: AnalysisInputResponse = {
  disease_id: 'MONDO_0004975',
  disease_name: 'Alzheimer disease',
  target_mode: 'specified',
  target_name: 'BACE1',
  original_smiles: 'C(C)O',
  canonical_smiles: 'CCO',
}

describe('AnalysisRunPanel', () => {
  it('keeps progress compact when Target is complete and other stages fail', async () => {
    const response = { ...analysisResponse('failed'), target_prioritization: targetFixture }
    response.stages[0].status = 'completed'
    response.input = {
      ...input,
      disease_id: 'MONDO_0007254',
      disease_name: 'breast cancer',
      target_name: 'CDK4',
    }
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(response, 202))
    vi.stubGlobal('fetch', fetchMock)
    const onViewResults = vi.fn()
    render(<AnalysisRunPanel input={response.input} onStartNew={vi.fn()} onViewResults={onViewResults} />)
    await clickAndFlush(screen.getByRole('button', { name: '실제 분석 실행' }))
    expect(screen.queryByRole('heading', { name: 'CDK4' })).not.toBeInTheDocument()
    expect(screen.getByText('분석 실행을 완료하지 못했습니다.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '결과 보기' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '결과 보기' }))
    expect(onViewResults).toHaveBeenCalledWith(response.analysis_id)
    await advancePollingTimer()
    expect(fetchMock).toHaveBeenCalledOnce()
  })
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('creates a real job, polls persisted state, and stops at terminal status', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(analysisResponse('queued'), 202))
      .mockResolvedValueOnce(jsonResponse(analysisResponse('running')))
      .mockResolvedValueOnce(jsonResponse(analysisResponse('completed')))
    vi.stubGlobal('fetch', fetchMock)

    render(<AnalysisRunPanel input={input} onStartNew={vi.fn()} onViewResults={vi.fn()} />)
    expect(screen.getByText('Live workflow')).toBeInTheDocument()

    await clickAndFlush(screen.getByRole('button', { name: '실제 분석 실행' }))
    expect(screen.getByText('Worker가 작업을 가져가기를 기다리고 있습니다.')).toBeInTheDocument()
    expect(screen.getByText('11111111-1111-4111-8111-111111111111')).toBeInTheDocument()

    await advancePollingTimer()
    expect(screen.getByText('서버에서 분석 단계를 실행하고 있습니다.')).toBeInTheDocument()

    await advancePollingTimer()
    expect(screen.getByText('검토 가능한 분석 결과가 준비되었습니다.')).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: '실제 분석 진행률' })).toHaveAttribute(
      'aria-valuenow',
      '100',
    )
    expect(fetchMock).toHaveBeenCalledTimes(3)

    await advancePollingTimer()
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('keeps polling after one transient failure and recovers on the next response', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(analysisResponse('queued'), 202))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(jsonResponse(analysisResponse('completed')))
    vi.stubGlobal('fetch', fetchMock)

    render(<AnalysisRunPanel input={input} onStartNew={vi.fn()} onViewResults={vi.fn()} />)
    await clickAndFlush(screen.getByRole('button', { name: '실제 분석 실행' }))
    await advancePollingTimer()

    expect(screen.getByText(/상태 연결이 불안정해 다시 확인하고 있습니다/)).toBeInTheDocument()
    await advancePollingTimer(5_000)

    expect(screen.getByText('검토 가능한 분석 결과가 준비되었습니다.')).toBeInTheDocument()
    expect(screen.queryByText(/상태 연결이 불안정해/)).not.toBeInTheDocument()
  })

  it('surfaces a persisted orchestration failure without presenting a result', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(analysisResponse('failed'), 202))
    vi.stubGlobal('fetch', fetchMock)

    const onStartNew = vi.fn()
    render(<AnalysisRunPanel input={input} onStartNew={onStartNew} onViewResults={vi.fn()} />)
    await clickAndFlush(screen.getByRole('button', { name: '실제 분석 실행' }))

    expect(screen.getByText('분석 실행을 완료하지 못했습니다.')).toBeInTheDocument()
    expect(screen.getByText('서버 오류 코드: agent_not_configured')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '새 작업 시작' })).toBeEnabled()

    await clickAndFlush(screen.getByRole('button', { name: '새 작업 시작' }))

    expect(onStartNew).toHaveBeenCalledOnce()
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('reuses the idempotency key when job creation is retried', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ detail: { code: 'api_busy', message: 'Busy.' } }, 503))
      .mockResolvedValueOnce(jsonResponse(analysisResponse('queued'), 202))
    vi.stubGlobal('fetch', fetchMock)

    render(<AnalysisRunPanel input={input} onStartNew={vi.fn()} onViewResults={vi.fn()} />)
    await clickAndFlush(screen.getByRole('button', { name: '실제 분석 실행' }))
    expect(screen.getByText(/다른 요청을 처리 중입니다/)).toBeInTheDocument()

    await clickAndFlush(screen.getByRole('button', { name: '생성 다시 시도' }))

    const firstHeaders = fetchMock.mock.calls[0][1]?.headers as Record<string, string>
    const secondHeaders = fetchMock.mock.calls[1][1]?.headers as Record<string, string>
    expect(firstHeaders['Idempotency-Key']).toBe(secondHeaders['Idempotency-Key'])
    expect(screen.getByText('Worker가 작업을 가져가기를 기다리고 있습니다.')).toBeInTheDocument()
  })

  it('stops automatic polling after three consecutive failures and allows recovery', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(analysisResponse('queued'), 202))
      .mockRejectedValueOnce(new Error('offline 1'))
      .mockRejectedValueOnce(new Error('offline 2'))
      .mockRejectedValueOnce(new Error('offline 3'))
      .mockResolvedValueOnce(jsonResponse(analysisResponse('completed')))
    vi.stubGlobal('fetch', fetchMock)

    render(<AnalysisRunPanel input={input} onStartNew={vi.fn()} onViewResults={vi.fn()} />)
    await clickAndFlush(screen.getByRole('button', { name: '실제 분석 실행' }))
    await advancePollingTimer(2_500)
    await advancePollingTimer(5_000)
    await advancePollingTimer(10_000)

    expect(screen.getByRole('alert')).toHaveTextContent('분석 상태를 계속 확인할 수 없습니다')
    expect(fetchMock).toHaveBeenCalledTimes(4)

    fireEvent.click(screen.getByRole('button', { name: '상태 다시 확인' }))
    await advancePollingTimer(2_500)
    expect(screen.getByText('검토 가능한 분석 결과가 준비되었습니다.')).toBeInTheDocument()
  })
})

async function clickAndFlush(element: HTMLElement) {
  await act(async () => {
    fireEvent.click(element)
    await Promise.resolve()
    await Promise.resolve()
  })
}

async function advancePollingTimer(duration = 2_500) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(duration)
  })
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function analysisResponse(status: 'queued' | 'running' | 'completed' | 'failed') {
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
