import { useEffect, useState, type ReactNode } from 'react'
import { AnalysisRequestError, readAnalysis, type Analysis } from './api'
import { readSpecialistResults, type SpecialistResults } from './specialistResults'
import { SpecialistResultsPanel } from './SpecialistResultsPanel'

export function SpecialistResultsLoader({ analysisId, afterDecision }: { analysisId: string; afterDecision?: ReactNode }) {
  return <ResultRequest key={analysisId} analysisId={analysisId} afterDecision={afterDecision} />
}

function ResultRequest({ analysisId, afterDecision }: { analysisId: string; afterDecision?: ReactNode }) {
  const [data, setData] = useState<SpecialistResults | null>(null)
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    let current = true
    Promise.all([
      readSpecialistResults(analysisId, controller.signal),
      readAnalysis(analysisId, controller.signal),
    ]).then(([result, analysis]) => {
      if (current) {
        setData(result)
        setAnalysis(analysis)
      }
    }).catch((error: unknown) => {
      if (!current) return
      const code = error instanceof AnalysisRequestError ? error.code : null
      setError(code === 'invalid_session' ? '인증이 필요합니다. 다시 로그인한 뒤 확인해 주세요.'
        : code === 'analysis_not_found' ? '이 브라우저에서 조회할 수 없는 분석입니다. ID를 확인해 주세요.'
          : '저장 결과를 불러오지 못했습니다. 연결 또는 응답 형식을 확인해 주세요. 분석 실패를 의미하지 않습니다.')
    })
    return () => { current = false; controller.abort() }
  }, [analysisId, attempt])
  if (error) return <div className="specialist-results"><p role="alert">{error}</p>
    <button type="button" className="secondary-button" onClick={() => { setError(null); setAttempt(attempt + 1) }}>결과 조회 재시도</button></div>
  if (!data || !analysis) return <p role="status">저장 결과를 불러오는 중입니다…</p>
  return <SpecialistResultsPanel data={data} analysis={analysis} afterDecision={afterDecision} />
}
