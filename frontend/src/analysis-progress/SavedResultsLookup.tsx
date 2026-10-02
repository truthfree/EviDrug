import { useEffect, useId, useState } from 'react'
import { AnalysisRequestError } from './api'
import { readRecentAnalyses, type RecentAnalysis } from './recentAnalyses'
import { isAnalysisId } from './specialistResults'
import './SpecialistResults.css'

const statusLabels: Record<RecentAnalysis['status'], string> = {
  queued: '대기 중',
  running: '진행 중',
  completed: '완료',
  partial_failure: '부분 완료',
  failed: '실패',
}

export function SavedResultsLookup({ onSelectAnalysis }: { onSelectAnalysis: (analysisId: string) => void }) {
  const id = useId()
  const [input, setInput] = useState('')
  const [error, setError] = useState(false)
  const [recent, setRecent] = useState<RecentAnalysis[] | null>(null)
  const [recentError, setRecentError] = useState<string | null>(null)
  const [refreshVersion, setRefreshVersion] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    let current = true
    setRecent(null)
    setRecentError(null)
    readRecentAnalyses(controller.signal).then((items) => {
      if (current) setRecent(items)
    }).catch((cause: unknown) => {
      if (!current) return
      const code = cause instanceof AnalysisRequestError ? cause.code : null
      setRecentError(code === 'invalid_session'
        ? '로그인이 만료되었습니다. 다시 로그인한 뒤 목록을 새로고침해 주세요.'
        : '최근 기록을 불러오지 못했습니다. 다시 시도하거나 분석 ID로 조회해 주세요.')
    })
    return () => { current = false; controller.abort() }
  }, [refreshVersion])

  return <section id="stored-results" className="specialist-results specialist-results__lookup" aria-label="저장 결과 조회">
    <h2>저장 결과 조회</h2>
    <p>이 브라우저에서 실행한 최근 분석입니다. 결과를 선택하면 별도 페이지에서 저장된 내용을 확인합니다.</p>
    <div className="recent-analyses__heading">
      <h3>최근 분석</h3>
      <button className="secondary-button" type="button" onClick={() => setRefreshVersion((version) => version + 1)}>목록 새로고침</button>
    </div>
    {recent === null && recentError === null && <p role="status">최근 기록을 불러오는 중입니다…</p>}
    {recentError && <p role="alert">{recentError}</p>}
    {recent?.length === 0 && <p>이 브라우저에 연결된 최근 분석이 없습니다. 현재 세션에서 실행한 기록의 ID를 알고 있다면 직접 조회할 수 있습니다.</p>}
    {recent && recent.length > 0 && <ul className="recent-analyses__list">
      {recent.map((item) => <li key={item.analysis_id} className="recent-analyses__item">
        <div className="recent-analyses__meta">
          <strong>{item.disease_name}</strong>
          <span>{statusLabels[item.status]}</span>
          <time dateTime={item.created_at}>{formatCreatedAt(item.created_at)}</time>
        </div>
        <dl>
          <div><dt>타깃 방식</dt><dd>{item.target_mode === 'discover' ? '표적 탐색' : `지정 표적 · ${item.target_name ?? '이름 없음'}`}</dd></div>
          <div><dt>화합물 · SMILES</dt><dd className="recent-analyses__smiles">{item.canonical_smiles}</dd></div>
        </dl>
        <button className="secondary-button" type="button" onClick={() => onSelectAnalysis(item.analysis_id)}>결과 보기</button>
      </li>)}
    </ul>}
    <h3>분석 ID로 조회</h3>
    <p>목록에 없는 기록의 ID를 알고 있다면 직접 입력할 수 있습니다.</p>
    <form onSubmit={(event) => {
      event.preventDefault()
      const value = input.trim().toLowerCase()
      setError(!isAnalysisId(value))
      if (isAnalysisId(value)) onSelectAnalysis(value)
    }}>
      <label htmlFor={id}>분석 ID</label>
      <input id={id} value={input} onChange={(event) => setInput(event.target.value)}
        placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" required aria-invalid={error} aria-describedby={error ? `${id}-error` : undefined} />
      <button className="secondary-button" type="submit">저장 결과 조회</button>
      {error && <p id={`${id}-error`} role="alert">올바른 UUID 형식의 분석 ID를 입력해 주세요.</p>}
    </form>
  </section>
}

function formatCreatedAt(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '시각 정보 없음'
  return new Intl.DateTimeFormat('ko-KR', {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(date)
}
