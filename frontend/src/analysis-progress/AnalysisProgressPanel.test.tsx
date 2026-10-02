import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AnalysisProgressPanel } from './AnalysisProgressPanel'

const frameDurationMs = 900

describe('AnalysisProgressPanel', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('marks the preview as demo data and plays the successful DAG', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    render(<AnalysisProgressPanel />)

    expect(screen.getByText('Demo data')).toBeInTheDocument()
    expect(
      screen.getByText(/실제 분석을 실행하거나 과학적 판정을 생성하지 않습니다/),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '전체 성공' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    fireEvent.click(screen.getByRole('button', { name: '목업 실행' }))
    await advanceFrame()

    expect(stageNamed('Target Hypothesis')).toHaveTextContent('실행 중')
    expect(stageNamed('ADMET')).toHaveTextContent('실행 중')
    expect(stageNamed('DTA')).toHaveTextContent('대기')

    await advanceFrame()

    expect(stageNamed('Target Hypothesis')).toHaveTextContent('완료')
    expect(stageNamed('ADMET')).toHaveTextContent('실행 중')
    expect(stageNamed('DTA')).toHaveTextContent('실행 중')
    expect(screen.getByRole('heading', { name: 'CDK4' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'PIK3CA' })).toBeInTheDocument()
    expect(screen.getByText(/합성 대안·제외 후보/)).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()

    await advanceFrame()
    expect(stageNamed('Decision')).toHaveTextContent('실행 중')

    await advanceFrame()
    expect(screen.getByText('검토 가능한 근거가 모두 준비되었습니다.')).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: '분석 진행률' })).toHaveAttribute(
      'aria-valuenow',
      '100',
    )
  })

  it('shows a skipped DTA as a data gap rather than a negative result', async () => {
    render(<AnalysisProgressPanel />)

    fireEvent.click(screen.getByRole('button', { name: '부분 실패' }))
    fireEvent.click(screen.getByRole('button', { name: '목업 실행' }))
    await advanceFrame()
    await advanceFrame()

    expect(stageNamed('DTA')).toHaveTextContent('건너뜀')
    expect(stageNamed('DTA')).toHaveTextContent('표적 서열이 없어')
    expect(stageNamed('Decision')).toHaveTextContent('실행 중')

    await advanceFrame()
    expect(screen.getByText('현재 근거로 조건부 검토가 가능합니다.')).toBeInTheDocument()
    expect(screen.getByText('부분 완료')).toBeInTheDocument()
  })

  it('stops before Decision when no specialist evidence is available', async () => {
    render(<AnalysisProgressPanel />)

    fireEvent.click(screen.getByRole('button', { name: '전체 실패' }))
    fireEvent.click(screen.getByRole('button', { name: '목업 실행' }))
    await advanceFrame()
    await advanceFrame()

    expect(stageNamed('Target Hypothesis')).toHaveTextContent('실패')
    expect(stageNamed('ADMET')).toHaveTextContent('실패')
    expect(stageNamed('Decision')).toHaveTextContent('건너뜀')
    expect(screen.getByText('이번 실행에서는 판정을 만들 수 없습니다.')).toBeInTheDocument()
  })

  it('returns a running scenario to its initial frame', async () => {
    render(<AnalysisProgressPanel />)

    fireEvent.click(screen.getByRole('button', { name: '목업 실행' }))
    await advanceFrame()
    fireEvent.click(screen.getByRole('button', { name: '처음으로' }))

    expect(screen.getByRole('progressbar', { name: '분석 진행률' })).toHaveAttribute(
      'aria-valuenow',
      '0',
    )
    expect(screen.getAllByText('실행 전').length).toBeGreaterThan(0)
  })
})

function stageNamed(name: string): HTMLElement {
  const heading = screen.getByRole('heading', { name })
  const stage = heading.closest('article')
  if (!stage) throw new Error(`${name} stage is missing`)
  within(stage).getByRole('heading', { name })
  return stage
}

async function advanceFrame() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(frameDurationMs)
  })
}
