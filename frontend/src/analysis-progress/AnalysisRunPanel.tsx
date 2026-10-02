import type { AnalysisInputResponse } from '../analysis-input/api'
import type { Analysis, AnalysisStatus } from './api'
import type { AnalysisStageName, AnalysisStageStatus } from './contracts'
import { useAnalysisRun } from './useAnalysisRun'
import './AnalysisProgressPanel.css'

const stageLabels: Record<AnalysisStageName, string> = {
  target_hypothesis: 'Target Hypothesis',
  admet: 'ADMET',
  dta: 'DTA',
  decision: 'Decision',
}

const stageNumbers: Record<AnalysisStageName, string> = {
  target_hypothesis: '01',
  admet: '02',
  dta: '03',
  decision: '04',
}

const statusLabels: Record<AnalysisStageStatus, string> = {
  pending: '대기',
  running: '실행 중',
  completed: '완료',
  failed: '실패',
  skipped: '건너뜀',
}

const analysisStatusLabels: Record<AnalysisStatus | 'ready', string> = {
  ready: '실행 전',
  queued: '대기 중',
  running: '분석 중',
  completed: '완료',
  partial_failure: '부분 완료',
  failed: '실패',
}

const initialStages: ReadonlyArray<{ name: AnalysisStageName; status: AnalysisStageStatus }> = [
  { name: 'target_hypothesis', status: 'pending' },
  { name: 'admet', status: 'pending' },
  { name: 'dta', status: 'pending' },
  { name: 'decision', status: 'pending' },
]

/** 검증된 입력으로 실제 분석 작업을 만들고 PostgreSQL 상태를 polling한다. */
type AnalysisRunPanelProps = {
  input: AnalysisInputResponse
  onStartNew: () => void
  onViewResults: (analysisId: string) => void
}

export function AnalysisRunPanel({ input, onStartNew, onViewResults }: AnalysisRunPanelProps) {
  const run = useAnalysisRun(input)
  const stages = run.analysis?.stages ?? initialStages
  const status = run.analysis?.status ?? 'ready'
  const progress = progressFor(run.analysis)
  const isBusy = run.phase === 'creating' || run.phase === 'polling'

  return (
    <section className="analysis-progress analysis-progress--live" aria-labelledby="live-run-title">
      <header className="analysis-progress__heading">
        <div>
          <div className="analysis-progress__kicker analysis-progress__kicker--live">
            <span>Live workflow</span>
            <p className="section-label">Persistent analysis</p>
          </div>
          <h2 id="live-run-title">확인한 입력으로 실제 분석 작업을 시작합니다.</h2>
          <p className="analysis-progress__description">
            서버가 저장한 단계 상태를 주기적으로 확인합니다. Agent가 구성되지 않은 환경에서는 실행이
            실패할 수 있으며, 실패 상태를 성공처럼 표시하지 않습니다.
          </p>
        </div>
        <div className="analysis-progress__sample">
          <span>확인된 입력</span>
          <strong>
            {input.disease_name}
            {input.target_name ? ` · ${input.target_name}` : ' · 타깃 탐색'}
          </strong>
          <code>{input.canonical_smiles}</code>
          <span>
            DTA 기준 · Kd ≤ {input.potency_criterion?.maximum_value ?? 100}{' '}
            {input.potency_criterion?.unit ?? 'nM'}
          </span>
        </div>
      </header>

      <div className="analysis-progress__run">
        <div className="analysis-progress__summary" aria-live="polite">
          <div>
            <span className={`analysis-progress__overall analysis-progress__overall--${status}`}>
              {analysisStatusLabels[status]}
            </span>
            <strong>{headlineFor(run.analysis, run.phase)}</strong>
          </div>
          <span>{progress}%</span>
        </div>
        <div
          className="analysis-progress__bar"
          role="progressbar"
          aria-label="실제 분석 진행률"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress}
        >
          <span style={{ width: `${progress}%` }} />
        </div>

        {run.analysis && (
          <div className="analysis-progress__run-id">
            <span>Analysis ID</span>
            <code>{run.analysis.analysis_id}</code>
          </div>
        )}

        <div className="analysis-progress__stages" aria-label="실제 분석 단계 상태">
          {stages.map((stage) => (
            <article className={`analysis-stage analysis-stage--${stage.status}`} key={stage.name}>
              <div className="analysis-stage__meta">
                <span>{stageNumbers[stage.name]}</span>
                <span>{statusLabels[stage.status]}</span>
              </div>
              <div className="analysis-stage__indicator" aria-hidden="true">
                {statusSymbol(stage.status)}
              </div>
              <h3>{stageLabels[stage.name]}</h3>
              <p>{stageDetail(stage.name, stage.status)}</p>
            </article>
          ))}
        </div>

        <div className="analysis-progress__footer">
          <div className="analysis-progress__activity">
            <span>실행 기록</span>
            {run.analysis?.events.length ? (
              <ul>
                {run.analysis.events.slice(-4).map((event) => (
                  <li key={`${event.event_type}-${event.created_at}`}>
                    {eventLabel(event.event_type, event.reason_code)}
                  </li>
                ))}
              </ul>
            ) : (
              <p>실행을 시작하면 서버가 기록한 상태 변경을 표시합니다.</p>
            )}
          </div>
          <div className="analysis-progress__actions">
            {run.phase === 'terminal' && run.analysis && (
              <button className="primary-button" type="button" onClick={() => {
                if (run.analysis) onViewResults(run.analysis.analysis_id)
              }}>
                결과 보기
              </button>
            )}
            {run.phase === 'error' && (
              <button className="secondary-button" type="button" onClick={run.retry}>
                {run.analysis ? '상태 다시 확인' : '생성 다시 시도'}
              </button>
            )}
            {run.phase !== 'error' && (
              <button
                className={run.phase === 'terminal' ? 'secondary-button' : 'primary-button'}
                type="button"
                disabled={isBusy}
                onClick={run.phase === 'terminal' ? onStartNew : run.start}
              >
                {run.phase === 'creating'
                  ? '작업 생성 중'
                  : run.phase === 'polling'
                    ? '분석 실행 중'
                    : run.phase === 'terminal'
                      ? '새 작업 시작'
                      : '실제 분석 실행'}
              </button>
            )}
          </div>
        </div>

        {run.message && (
          <p
            className="analysis-progress__message"
            role={run.phase === 'error' ? 'alert' : 'status'}
          >
            {run.message}
          </p>
        )}

        {run.phase === 'terminal' && run.analysis && (
          <div
            className={`analysis-progress__outcome analysis-progress__outcome--${run.analysis.status}`}
          >
            <span>{analysisStatusLabels[run.analysis.status]}</span>
            <div>
              <strong>{terminalTitle(run.analysis.status)}</strong>
              <p>{terminalDescription(run.analysis)}</p>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}

function progressFor(analysis: Analysis | null): number {
  if (analysis === null) return 0
  if (analysis.status === 'queued') return 8
  if (analysis.status !== 'running') return 100
  const terminalCount = analysis.stages.filter((stage) =>
    ['completed', 'failed', 'skipped'].includes(stage.status),
  ).length
  const runningCount = analysis.stages.filter((stage) => stage.status === 'running').length
  return Math.min(10 + terminalCount * 20 + runningCount * 10, 90)
}

function headlineFor(analysis: Analysis | null, phase: string): string {
  if (phase === 'creating') return '분석 작업을 저장하고 있습니다.'
  if (phase === 'error') return '상태 확인에 사용자 조치가 필요합니다.'
  if (analysis === null) return '검증된 입력이 준비되었습니다.'
  if (analysis.status === 'queued') return 'Worker가 작업을 가져가기를 기다리고 있습니다.'
  if (analysis.status === 'running') return '서버에서 분석 단계를 실행하고 있습니다.'
  if (analysis.status === 'completed') return '모든 분석 단계를 완료했습니다.'
  if (analysis.status === 'partial_failure') return '일부 데이터 공백을 남긴 채 완료했습니다.'
  return '이번 실행에서는 판정을 만들 수 없습니다.'
}

function stageDetail(name: AnalysisStageName, status: AnalysisStageStatus): string {
  if (status === 'pending') return '선행 조건과 실행 순서를 기다리고 있습니다.'
  if (status === 'running') return '서버가 이 단계를 실행하고 있습니다.'
  if (status === 'completed') return '이 단계의 결과가 저장되었습니다.'
  if (status === 'failed') return '이 단계가 실패했습니다. 실행 기록에서 원인을 확인해야 합니다.'
  if (name === 'dta') return '필요한 표적 입력이 없어 실행하지 않았을 수 있습니다.'
  return '실행 조건이 충족되지 않아 이 단계를 건너뛰었습니다.'
}

function eventLabel(eventType: string, reasonCode: string | null): string {
  const labels: Record<string, string> = {
    analysis_created: '분석 작업 생성',
    analysis_started: '분석 실행 시작',
    analysis_completed: '분석 완료',
    analysis_partial_failure: '부분 실패로 완료',
    analysis_failed: '분석 실패',
    queue_dispatch_failed: '작업 전달 실패',
    orchestration_lease_expired: '실행 lease 만료',
  }
  const label = labels[eventType] ?? eventType
  return reasonCode ? `${label} · ${reasonCode}` : label
}

function terminalTitle(status: AnalysisStatus): string {
  if (status === 'completed') return '검토 가능한 분석 결과가 준비되었습니다.'
  if (status === 'partial_failure') return '현재 근거로 조건부 검토가 가능합니다.'
  return '분석 실행을 완료하지 못했습니다.'
}

function terminalDescription(analysis: Analysis): string {
  if (analysis.error_code) return `서버 오류 코드: ${analysis.error_code}`
  if (analysis.status === 'partial_failure') {
    return '완료되지 않은 단계와 데이터 공백을 확인한 뒤 결과를 해석해야 합니다.'
  }
  if (analysis.status === 'completed')
    return '결과 보기에서 각 단계의 저장 결과와 근거를 확인하세요.'
  return '실행 기록을 확인한 뒤 같은 입력으로 새 작업을 시작할 수 있습니다.'
}

function statusSymbol(status: AnalysisStageStatus): string {
  if (status === 'completed') return '✓'
  if (status === 'failed') return '!'
  if (status === 'skipped') return '—'
  if (status === 'running') return '•••'
  return '·'
}
