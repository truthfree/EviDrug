import type { AnalysisStageName, AnalysisStageStatus, MockAnalysisStatus } from './contracts'
import { mockScenarioList } from './mockScenarios'
import { useMockAnalysis } from './useMockAnalysis'
import { TargetPrioritizationPanel } from './TargetPrioritizationPanel'
import { targetDemo } from './targetDemo'
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

const analysisStatusLabels: Record<MockAnalysisStatus, string> = {
  ready: '실행 전',
  running: '분석 중',
  completed: '완료',
  partial_failure: '부분 완료',
  failed: '실패',
}

/** 백엔드 fixed DAG를 실제 API 호출 없이 검토하는 사용자 화면 목업이다. */
export function AnalysisProgressPanel() {
  const analysis = useMockAnalysis()
  const { frame, scenario } = analysis

  return (
    <section className="analysis-progress" id="analysis-preview" aria-labelledby="progress-title">
      <header className="analysis-progress__heading">
        <div>
          <div className="analysis-progress__kicker">
            <span>Demo data</span>
            <p className="section-label">Orchestration preview</p>
          </div>
          <h2 id="progress-title">분석이 진행되는 모습을 미리 확인하세요.</h2>
          <p>
            아래 상태와 결과는 화면 검토용 시나리오입니다. 실제 분석을 실행하거나 과학적 판정을
            생성하지 않습니다.
          </p>
        </div>
        <div className="analysis-progress__sample">
          <span>예시 분석</span>
          <strong>breast cancer · CDK4</strong>
          <span>예시 약물: palbociclib</span>
          <code>CC(=O)c1c(C)c2cnc(Nc3ccc(N4CCNCC4)cn3)nc2n(C2CCCC2)c1=O</code>
        </div>
      </header>

      <div className="analysis-progress__scenario" aria-label="목업 시나리오 선택">
        <div>
          <span>실행 시나리오</span>
          <p>{scenario.description}</p>
        </div>
        <div className="analysis-progress__scenario-options">
          {mockScenarioList.map((option) => (
            <button
              key={option.id}
              type="button"
              aria-pressed={scenario.id === option.id}
              onClick={() => analysis.selectScenario(option.id)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <div className="analysis-progress__run">
        <div className="analysis-progress__summary" aria-live="polite">
          <div>
            <span
              className={`analysis-progress__overall analysis-progress__overall--${frame.status}`}
            >
              {analysisStatusLabels[frame.status]}
            </span>
            <strong>{frame.headline}</strong>
          </div>
          <span>{frame.progress}%</span>
        </div>
        <div
          className="analysis-progress__bar"
          role="progressbar"
          aria-label="분석 진행률"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={frame.progress}
        >
          <span style={{ width: `${frame.progress}%` }} />
        </div>

        <div className="analysis-progress__stages" aria-label="분석 단계 상태">
          {frame.stages.map((stage) => (
            <article className={`analysis-stage analysis-stage--${stage.status}`} key={stage.name}>
              <div className="analysis-stage__meta">
                <span>{stageNumbers[stage.name]}</span>
                <span>{statusLabels[stage.status]}</span>
              </div>
              <div className="analysis-stage__indicator" aria-hidden="true">
                {statusSymbol(stage.status)}
              </div>
              <h3>{stageLabels[stage.name]}</h3>
              <p>{stage.detail}</p>
            </article>
          ))}
        </div>

        {scenario.id === 'success' &&
          frame.stages.some(
            (stage) => stage.name === 'target_hypothesis' && stage.status === 'completed',
          ) && <TargetPrioritizationPanel result={targetDemo} demo />}

        <div className="analysis-progress__footer">
          <div className="analysis-progress__activity">
            <span>최근 실행 기록</span>
            <ul>
              {frame.activity.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
          <div className="analysis-progress__actions">
            <button
              className="secondary-button"
              type="button"
              disabled={frame.status === 'ready'}
              onClick={analysis.reset}
            >
              처음으로
            </button>
            <button
              className="primary-button"
              type="button"
              disabled={analysis.isPlaying}
              onClick={analysis.play}
            >
              {analysis.isPlaying
                ? '시나리오 실행 중'
                : analysis.isTerminal
                  ? '다시 실행'
                  : '목업 실행'}
            </button>
          </div>
        </div>

        {analysis.isTerminal && (
          <div className={`analysis-progress__outcome analysis-progress__outcome--${frame.status}`}>
            <span>{scenario.outcome.label}</span>
            <div>
              <strong>{scenario.outcome.title}</strong>
              <p>{scenario.outcome.description}</p>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}

function statusSymbol(status: AnalysisStageStatus): string {
  if (status === 'completed') return '✓'
  if (status === 'failed') return '!'
  if (status === 'skipped') return '—'
  if (status === 'running') return '•••'
  return '·'
}
