export type AnalysisStageName = 'target_hypothesis' | 'admet' | 'dta' | 'decision'

export type AnalysisStageStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped'

export type MockAnalysisStatus =
  | 'ready'
  | 'running'
  | 'completed'
  | 'partial_failure'
  | 'failed'

export type MockStage = {
  name: AnalysisStageName
  status: AnalysisStageStatus
  detail: string
}

export type MockFrame = {
  status: MockAnalysisStatus
  progress: number
  headline: string
  stages: readonly MockStage[]
  activity: readonly string[]
}

export type MockScenarioId = 'success' | 'partial' | 'failed'

export type MockScenario = {
  id: MockScenarioId
  label: string
  description: string
  frames: readonly MockFrame[]
  outcome: {
    label: string
    title: string
    description: string
  }
}
