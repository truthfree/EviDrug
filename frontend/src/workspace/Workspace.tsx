import { useEffect, useState, type MouseEvent } from 'react'
import { AnalysisInputPanel } from '../analysis-input/AnalysisInputPanel'
import type { AnalysisInputResponse } from '../analysis-input/api'
import { AnalysisProgressPanel } from '../analysis-progress/AnalysisProgressPanel'
import { AnalysisRunPanel } from '../analysis-progress/AnalysisRunPanel'
import { SavedResultsLookup } from '../analysis-progress/SavedResultsLookup'
import { SpecialistResultsLoader } from '../analysis-progress/SpecialistResultsLoader'
import { TargetResultsLoader } from '../analysis-progress/TargetResultsLoader'
import { isAnalysisId } from '../analysis-progress/specialistResults'
import { Brand } from '../shared/Brand'
import type { DiseaseCandidate } from '../disease-search/api'
import { DiseaseSearchPanel } from '../disease-search/DiseaseSearchPanel'
import './Workspace.css'

type WorkspaceProps = {
  expiresAt: string
  isSigningOut: boolean
  signOutError: string | null
  onSignOut: () => Promise<void>
}

type WorkspacePage = 'analysis' | 'preview' | 'results' | 'result-detail'

const pagePaths: Record<Exclude<WorkspacePage, 'result-detail'>, string> = {
  analysis: '/',
  preview: '/preview',
  results: '/results',
}

function pageFromPath(pathname: string): WorkspacePage {
  if (pathname === pagePaths.preview) return 'preview'
  if (pathname === pagePaths.results) return 'results'
  if (pathname.startsWith(`${pagePaths.results}/`)) return 'result-detail'
  return 'analysis'
}

/** 인증 후 현재 준비된 기능과 다음 분석 단계를 안내하는 작업공간이다. */
export function Workspace({ expiresAt, isSigningOut, signOutError, onSignOut }: WorkspaceProps) {
  const [page, setPage] = useState<WorkspacePage>(() => pageFromPath(window.location.pathname))
  const [confirmedDisease, setConfirmedDisease] = useState<DiseaseCandidate | null>(null)
  const [validatedInput, setValidatedInput] = useState<AnalysisInputResponse | null>(null)
  const [inputResetVersion, setInputResetVersion] = useState(0)
  const [activeRunResultId, setActiveRunResultId] = useState<string | null>(null)

  useEffect(() => {
    const onPopState = () => setPage(pageFromPath(window.location.pathname))
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  function navigate(event: MouseEvent<HTMLAnchorElement>, destination: Exclude<WorkspacePage, 'result-detail'>) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey
      || event.shiftKey || event.altKey) return
    event.preventDefault()
    if (page === destination && window.location.pathname === pagePaths[destination]) return
    window.history.pushState(null, '', pagePaths[destination])
    setPage(destination)
    document.querySelector('main')?.scrollIntoView?.({ block: 'start' })
  }

  function openResult(analysisId: string, fromRun = false) {
    setActiveRunResultId(fromRun ? analysisId : null)
    window.history.pushState(null, '', `${pagePaths.results}/${analysisId}`)
    setPage('result-detail')
    document.querySelector('main')?.scrollIntoView?.({ block: 'start' })
  }

  const resultId = page === 'result-detail'
    ? window.location.pathname.slice(`${pagePaths.results}/`.length)
    : null

  useEffect(() => {
    if (inputResetVersion === 0) return
    const diseaseInput = document.getElementById('disease-query')
    diseaseInput?.focus({ preventScroll: true })
    document.getElementById('disease-search-title')?.scrollIntoView?.({
      behavior: 'smooth',
      block: 'center',
    })
  }, [inputResetVersion])

  function handleConfirmedDiseaseChange(candidate: DiseaseCandidate | null) {
    setConfirmedDisease(candidate)
    setValidatedInput(null)
  }

  function handleStartNewAnalysis() {
    setConfirmedDisease(null)
    setValidatedInput(null)
    setActiveRunResultId(null)
    setInputResetVersion((version) => version + 1)
  }

  return (
    <div className="workspace-shell">
      <header className="workspace-header">
        <Brand />
        <nav aria-label="분석 작업공간">
          <a
            className={page === 'analysis' ? 'workspace-nav__active' : undefined}
            aria-current={page === 'analysis' ? 'page' : undefined}
            href="/"
            onClick={(event) => navigate(event, 'analysis')}
          >
            새 분석
          </a>
          <a
            className={page === 'preview' ? 'workspace-nav__active' : undefined}
            aria-current={page === 'preview' ? 'page' : undefined}
            href="/preview"
            onClick={(event) => navigate(event, 'preview')}
          >
            실행 목업
          </a>
          <a
            className={page === 'results' || page === 'result-detail' ? 'workspace-nav__active' : undefined}
            aria-current={page === 'results' || page === 'result-detail' ? 'page' : undefined}
            href="/results"
            onClick={(event) => navigate(event, 'results')}
          >
            저장 결과
          </a>
          <span>서비스 안내</span>
        </nav>
        <button
          className="secondary-button"
          type="button"
          disabled={isSigningOut}
          onClick={() => void onSignOut()}
        >
          {isSigningOut ? '로그아웃 중' : '로그아웃'}
        </button>
      </header>

      <main className="workspace-main">
        {signOutError && (
          <p className="workspace-error" role="alert">
            {signOutError}
          </p>
        )}

        <div hidden={page !== 'analysis'}>
          <WorkspaceIntroduction expiresAt={expiresAt} />

          <DiseaseSearchPanel
            key={inputResetVersion}
            onConfirmedDiseaseChange={handleConfirmedDiseaseChange}
          />
          {confirmedDisease && (
            <AnalysisInputPanel
              key={confirmedDisease.id}
              disease={confirmedDisease}
              onValidatedInputChange={setValidatedInput}
            />
          )}

          {validatedInput && (
            <AnalysisRunPanel
              key={[
                validatedInput.disease_id,
                validatedInput.target_mode,
                validatedInput.target_name,
                validatedInput.canonical_smiles,
              ].join(':')}
              input={validatedInput}
              onStartNew={handleStartNewAnalysis}
              onViewResults={(analysisId) => openResult(analysisId, true)}
            />
          )}

          <section className="workspace-flow" aria-label="예정된 분석 흐름">
            <WorkspaceStep number="01" title="질환 해석" state={confirmedDisease ? 'complete' : 'ready'} />
            <WorkspaceStep
              number="02" title="타깃 선택"
              state={validatedInput ? 'complete' : confirmedDisease ? 'ready' : 'waiting'}
            />
            <WorkspaceStep
              number="03" title="화합물 입력"
              state={validatedInput ? 'complete' : confirmedDisease ? 'ready' : 'waiting'}
            />
            <WorkspaceStep number="04" title="분석 확인" state={validatedInput ? 'ready' : 'waiting'} />
          </section>
        </div>

        {page === 'preview' && (
          <>
            <WorkspacePageIntroduction
              title="실행 목업"
              description="실제 분석과 분리된 화면에서 분석 흐름의 예시를 살펴보세요."
            />
            <AnalysisProgressPanel />
          </>
        )}

        {page === 'results' && (
          <>
            <WorkspacePageIntroduction
              title="저장 결과"
              description="이 브라우저의 최근 분석을 선택하거나 분석 ID로 저장 결과를 다시 조회하세요. 새로운 분석은 시작하지 않습니다."
            />
            <SavedResultsLookup onSelectAnalysis={openResult} />
          </>
        )}

        {page === 'result-detail' && (
          <>
            {activeRunResultId === resultId && validatedInput
              ? <a className="workspace-back-link" href="/" onClick={(event) => navigate(event, 'analysis')}>
                  ← 분석 진행 화면으로
                </a>
              : <a className="workspace-back-link" href="/results" onClick={(event) => navigate(event, 'results')}>
                  ← 저장 결과 목록으로
                </a>}
            <WorkspacePageIntroduction
              title="저장된 분석 결과"
              description="이전에 저장된 분석 결과를 확인합니다. 이 페이지에서는 새 분석을 실행하지 않습니다."
            />
            {resultId && isAnalysisId(resultId)
              ? <SpecialistResultsLoader
                  analysisId={resultId}
                  afterDecision={<TargetResultsLoader analysisId={resultId} />}
                />
              : <p className="workspace-error" role="alert">올바르지 않은 분석 ID입니다. 저장 결과 목록에서 다시 선택해 주세요.</p>}
          </>
        )}
      </main>
    </div>
  )
}

function WorkspacePageIntroduction({ title, description }: { title: string; description: string }) {
  return (
    <section className="workspace-intro">
      <div>
        <p className="eyebrow"><span aria-hidden="true" />Authenticated workspace</p>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
    </section>
  )
}

function WorkspaceIntroduction({ expiresAt }: { expiresAt: string }) {
  return (
    <section className="workspace-intro">
      <div>
        <p className="eyebrow">
          <span aria-hidden="true" />
          Authenticated workspace
        </p>
        <h1>
          <span>새로운 연구 질문을</span>{' '}
          <span>시작하세요.</span>
        </h1>
        <p>
          <span>질환을 먼저 입력하면 EviDrug이 가능한 해석을 제시하고,</span>{' '}
          <span>사용자의 확인을 받아 다음 단계로 이동합니다.</span>
        </p>
      </div>
      <div className="session-card">
        <span className="status-symbol status-symbol--success" aria-hidden="true">
          ✓
        </span>
        <div>
          <strong>안전하게 연결됨</strong>
          <span>세션 만료 {formatSessionExpiry(expiresAt)}</span>
        </div>
      </div>
    </section>
  )
}

type WorkspaceStepProps = {
  number: string
  title: string
  state: 'ready' | 'waiting' | 'complete'
}

function WorkspaceStep({ number, title, state }: WorkspaceStepProps) {
  return (
    <div className={`workspace-step workspace-step--${state}`}>
      <span>{number}</span>
      <strong>{title}</strong>
      <small>
        {state === 'complete' ? '확인 완료' : state === 'ready' ? '준비됨' : '이전 단계 후 진행'}
      </small>
    </div>
  )
}

function formatSessionExpiry(expiresAt: string): string {
  const expiry = new Date(expiresAt)
  if (Number.isNaN(expiry.getTime())) return '시간 확인 필요'

  return new Intl.DateTimeFormat('ko-KR', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(expiry)
}
