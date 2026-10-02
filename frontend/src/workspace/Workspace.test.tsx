import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AnalysisInputResponse } from '../analysis-input/api'
import type { DiseaseCandidate } from '../disease-search/api'
import { Workspace } from './Workspace'

const disease: DiseaseCandidate = {
  id: 'MONDO_0007254',
  name: 'breast cancer',
  description: null,
  relevance_score: 1,
}

const input: AnalysisInputResponse = {
  disease_id: disease.id,
  disease_name: disease.name,
  target_mode: 'discover',
  target_name: null,
  original_smiles: 'C(C)O',
  canonical_smiles: 'CCO',
}

vi.mock('../analysis-progress/AnalysisProgressPanel', () => ({
  AnalysisProgressPanel: () => <div>목업 콘텐츠</div>,
}))

vi.mock('../analysis-progress/SavedResultsLookup', () => ({
  SavedResultsLookup: ({ onSelectAnalysis }: { onSelectAnalysis: (id: string) => void }) => <section>
    <h2>저장 결과 조회</h2>
    <button type="button" onClick={() => onSelectAnalysis('11111111-1111-4111-8111-111111111111')}>
      결과 보기
    </button>
  </section>,
}))

vi.mock('../analysis-progress/SpecialistResultsLoader', () => ({
  SpecialistResultsLoader: ({ analysisId, afterDecision }: { analysisId: string; afterDecision?: React.ReactNode }) => <>
    <div>저장된 Decision {analysisId}</div>
    {afterDecision}
    <div>저장된 DTA {analysisId}</div>
    <div>저장된 ADMET {analysisId}</div>
  </>,
}))

vi.mock('../analysis-progress/TargetResultsLoader', () => ({
  TargetResultsLoader: ({ analysisId }: { analysisId: string }) => <div>저장된 Target {analysisId}</div>,
}))

vi.mock('../disease-search/DiseaseSearchPanel', () => ({
  DiseaseSearchPanel: ({
    onConfirmedDiseaseChange,
  }: {
    onConfirmedDiseaseChange: (candidate: DiseaseCandidate | null) => void
  }) => (
    <section>
      <h2 id="disease-search-title">질환 입력</h2>
      <input id="disease-query" aria-label="Disease or phenotype" />
      <button type="button" onClick={() => onConfirmedDiseaseChange(disease)}>
        질환 확정
      </button>
    </section>
  ),
}))

vi.mock('../analysis-input/AnalysisInputPanel', () => ({
  AnalysisInputPanel: ({
    onValidatedInputChange,
  }: {
    onValidatedInputChange: (input: AnalysisInputResponse | null) => void
  }) => (
    <button type="button" onClick={() => onValidatedInputChange(input)}>
      입력 확정
    </button>
  ),
}))

vi.mock('../analysis-progress/AnalysisRunPanel', async () => {
  const { useState } = await import('react')
  return {
    AnalysisRunPanel: ({ onStartNew, onViewResults }: { onStartNew: () => void; onViewResults: (id: string) => void }) => {
      const [progress, setProgress] = useState(0)
      return <>
        <button type="button" onClick={onStartNew}>새 작업 시작</button>
        <button type="button" onClick={() => setProgress((value) => value + 1)}>진행 상태 {progress}</button>
        <button type="button" onClick={() => onViewResults('11111111-1111-4111-8111-111111111111')}>실행 결과 보기</button>
      </>
    },
  }
})

describe('Workspace', () => {
  afterEach(() => {
    cleanup()
    window.history.replaceState(null, '', '/')
  })

  it('shows the mock and saved-result pages separately while keeping the analysis form mounted', () => {
    render(
      <Workspace expiresAt="2026-09-21T12:00:00Z" isSigningOut={false} signOutError={null} onSignOut={vi.fn()} />,
    )

    expect(screen.getByRole('link', { name: '새 분석' })).toHaveAttribute('aria-current', 'page')
    expect(screen.queryByText('목업 콘텐츠')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '저장 결과 조회' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '질환 확정' }))
    fireEvent.click(screen.getByRole('button', { name: '입력 확정' }))
    fireEvent.click(screen.getByRole('button', { name: '진행 상태 0' }))
    fireEvent.click(screen.getByRole('link', { name: '실행 목업' }))
    expect(window.location.pathname).toBe('/preview')
    expect(screen.getByRole('heading', { name: '실행 목업', level: 1 })).toBeInTheDocument()
    expect(screen.getByText('목업 콘텐츠')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '새 작업 시작' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('link', { name: '저장 결과' }))
    expect(window.location.pathname).toBe('/results')
    expect(screen.getByRole('heading', { name: '저장 결과 조회' })).toBeInTheDocument()
    expect(screen.queryByText('목업 콘텐츠')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('link', { name: '새 분석' }))
    expect(window.location.pathname).toBe('/')
    expect(screen.getByRole('button', { name: '새 작업 시작' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '진행 상태 1' })).toBeInTheDocument()
  })

  it('opens the saved-results page directly and follows browser history', () => {
    window.history.replaceState(null, '', '/results')
    render(
      <Workspace expiresAt="2026-09-21T12:00:00Z" isSigningOut={false} signOutError={null} onSignOut={vi.fn()} />,
    )
    expect(screen.getByRole('link', { name: '저장 결과' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('heading', { name: '저장 결과 조회' })).toBeInTheDocument()

    window.history.replaceState(null, '', '/preview')
    fireEvent.popState(window)
    expect(screen.getByRole('heading', { name: '실행 목업', level: 1 })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '저장 결과 조회' })).not.toBeInTheDocument()
  })

  it('opens a separate result URL and returns to the list with browser history', () => {
    window.history.replaceState(null, '', '/results')
    render(
      <Workspace expiresAt="2026-09-21T12:00:00Z" isSigningOut={false} signOutError={null} onSignOut={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '결과 보기' }))
    expect(window.location.pathname).toBe('/results/11111111-1111-4111-8111-111111111111')
    const specialistResults = screen.getByText('저장된 Decision 11111111-1111-4111-8111-111111111111')
    const targetResults = screen.getByText('저장된 Target 11111111-1111-4111-8111-111111111111')
    const dtaResults = screen.getByText('저장된 DTA 11111111-1111-4111-8111-111111111111')
    const admetResults = screen.getByText('저장된 ADMET 11111111-1111-4111-8111-111111111111')
    expect(specialistResults).toBeInTheDocument()
    expect(specialistResults.compareDocumentPosition(targetResults) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(targetResults.compareDocumentPosition(dtaResults) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(dtaResults.compareDocumentPosition(admetResults) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByRole('heading', { name: '저장 결과 조회' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: '저장 결과' })).toHaveAttribute('aria-current', 'page')

    window.history.replaceState(null, '', '/results')
    fireEvent.popState(window)
    expect(screen.getByRole('heading', { name: '저장 결과 조회' })).toBeInTheDocument()
    expect(screen.queryByText('저장된 Decision 11111111-1111-4111-8111-111111111111')).not.toBeInTheDocument()
  })

  it('opens completed run results and returns to the still-mounted progress screen', () => {
    render(
      <Workspace expiresAt="2026-09-21T12:00:00Z" isSigningOut={false} signOutError={null} onSignOut={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '질환 확정' }))
    fireEvent.click(screen.getByRole('button', { name: '입력 확정' }))
    fireEvent.click(screen.getByRole('button', { name: '진행 상태 0' }))
    fireEvent.click(screen.getByRole('button', { name: '실행 결과 보기' }))
    expect(window.location.pathname).toBe('/results/11111111-1111-4111-8111-111111111111')
    expect(screen.getByText('저장된 Target 11111111-1111-4111-8111-111111111111')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: '← 분석 진행 화면으로' }))
    expect(window.location.pathname).toBe('/')
    expect(screen.getByRole('button', { name: '진행 상태 1' })).toBeInTheDocument()
  })

  it('supports direct detail URLs and rejects malformed IDs without requesting results', () => {
    window.history.replaceState(null, '', '/results/11111111-1111-4111-8111-111111111111')
    render(
      <Workspace expiresAt="2026-09-21T12:00:00Z" isSigningOut={false} signOutError={null} onSignOut={vi.fn()} />,
    )
    expect(screen.getByText('저장된 Decision 11111111-1111-4111-8111-111111111111')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: '← 저장 결과 목록으로' }))
    expect(window.location.pathname).toBe('/results')
    expect(screen.getByRole('heading', { name: '저장 결과 조회' })).toBeInTheDocument()

    window.history.replaceState(null, '', '/results/not-an-id')
    fireEvent.popState(window)
    expect(screen.getByRole('alert')).toHaveTextContent('올바르지 않은 분석 ID')
    expect(screen.queryByText(/저장된 Decision/)).not.toBeInTheDocument()
  })

  it('clears every input and returns focus to disease search for a new analysis', () => {
    render(
      <Workspace
        expiresAt="2026-09-21T12:00:00Z"
        isSigningOut={false}
        signOutError={null}
        onSignOut={vi.fn()}
      />,
    )

    const diseaseInput = screen.getByRole('textbox', { name: 'Disease or phenotype' })
    fireEvent.change(diseaseInput, { target: { value: 'breast cancer' } })
    fireEvent.click(screen.getByRole('button', { name: '질환 확정' }))
    fireEvent.click(screen.getByRole('button', { name: '입력 확정' }))
    fireEvent.click(screen.getByRole('button', { name: '새 작업 시작' }))

    expect(screen.queryByRole('button', { name: '입력 확정' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '새 작업 시작' })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Disease or phenotype' })).toHaveValue('')
    expect(screen.getByRole('textbox', { name: 'Disease or phenotype' })).toHaveFocus()
  })
})
