import { useEffect, useState } from 'react'
import { AnalysisRequestError, readAnalysis, type Analysis } from './api'
import { TargetPrioritizationPanel } from './TargetPrioritizationPanel'

/** 저장된 Target 요약을 결과 페이지에서 조회한다. 새 분석은 생성하지 않는다. */
export function TargetResultsLoader({ analysisId }: { analysisId: string }) {
  return <TargetRequest key={analysisId} analysisId={analysisId} />
}

function TargetRequest({ analysisId }: { analysisId: string }) {
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    let current = true
    readAnalysis(analysisId, controller.signal).then((result) => {
      if (current) setAnalysis(result)
    }).catch((reason: unknown) => {
      if (!current) return
      const code = reason instanceof AnalysisRequestError ? reason.code : null
      setError(code === 'invalid_session' ? '인증이 필요합니다. 다시 로그인한 뒤 확인해 주세요.'
        : code === 'analysis_not_found' ? '분석을 찾을 수 없습니다. ID를 확인해 주세요.'
          : 'Target 결과를 불러오지 못했습니다. 연결 상태를 확인해 주세요.')
    })
    return () => { current = false; controller.abort() }
  }, [analysisId, attempt])

  if (error) return <div className="target-results"><p role="alert">{error}</p>
    <button type="button" className="secondary-button" onClick={() => { setError(null); setAttempt(attempt + 1) }}>Target 결과 다시 확인</button></div>
  if (!analysis) return <p role="status">Target 결과를 불러오는 중입니다…</p>
  return <TargetPrioritizationPanel result={analysis.target_prioritization ?? null} />
}
