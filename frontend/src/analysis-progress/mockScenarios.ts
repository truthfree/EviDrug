import type {
  AnalysisStageName,
  AnalysisStageStatus,
  MockFrame,
  MockScenario,
  MockScenarioId,
} from './contracts'

const stageDetails: Record<AnalysisStageName, string> = {
  target_hypothesis: '질환 맥락에서 표적과 단백질 서열을 확인합니다.',
  admet: '화합물의 흡수·분포·대사·배설·독성 근거를 살핍니다.',
  dta: '확인된 표적과 화합물 사이의 결합 예측을 검토합니다.',
  decision: '사용 가능한 근거와 데이터 공백을 함께 종합합니다.',
}

function frame(
  status: MockFrame['status'],
  progress: number,
  headline: string,
  states: Record<AnalysisStageName, AnalysisStageStatus>,
  activity: readonly string[],
  details: Partial<Record<AnalysisStageName, string>> = {},
): MockFrame {
  return {
    status,
    progress,
    headline,
    activity,
    stages: (Object.keys(stageDetails) as AnalysisStageName[]).map((name) => ({
      name,
      status: states[name],
      detail: details[name] ?? stageDetails[name],
    })),
  }
}

const pendingStages: Record<AnalysisStageName, AnalysisStageStatus> = {
  target_hypothesis: 'pending',
  admet: 'pending',
  dta: 'pending',
  decision: 'pending',
}

export const mockScenarios: Record<MockScenarioId, MockScenario> = {
  success: {
    id: 'success',
    label: '전체 성공',
    description: '모든 전문 단계가 완료되고 최종 판단까지 도달합니다.',
    frames: [
      frame('ready', 0, '실행 전', pendingStages, ['분석 입력 검증 완료']),
      frame(
        'running',
        24,
        '표적과 ADMET을 병렬로 분석하고 있습니다.',
        {
          target_hypothesis: 'running',
          admet: 'running',
          dta: 'pending',
          decision: 'pending',
        },
        ['Target Hypothesis 시작', 'ADMET 시작'],
      ),
      frame(
        'running',
        56,
        '표적 입력이 준비되어 DTA를 시작했습니다.',
        {
          target_hypothesis: 'completed',
          admet: 'running',
          dta: 'running',
          decision: 'pending',
        },
        ['Target Hypothesis 완료', 'DTA 시작', 'ADMET 분석 계속'],
      ),
      frame(
        'running',
        82,
        '전문 근거를 모아 최종 판단을 구성하고 있습니다.',
        {
          target_hypothesis: 'completed',
          admet: 'completed',
          dta: 'completed',
          decision: 'running',
        },
        ['ADMET 완료', 'DTA 완료', 'Decision 시작'],
      ),
      frame(
        'completed',
        100,
        '모든 분석 단계를 완료했습니다.',
        {
          target_hypothesis: 'completed',
          admet: 'completed',
          dta: 'completed',
          decision: 'completed',
        },
        ['Decision 완료', '분석 완료'],
      ),
    ],
    outcome: {
      label: 'Completed',
      title: '검토 가능한 근거가 모두 준비되었습니다.',
      description: '실제 화면에서는 최종 판정, 사용한 근거와 모델·데이터 출처를 표시합니다.',
    },
  },
  partial: {
    id: 'partial',
    label: '부분 실패',
    description: '표적 서열이 없어 DTA를 건너뛰고 남은 근거로 판단합니다.',
    frames: [
      frame('ready', 0, '실행 전', pendingStages, ['분석 입력 검증 완료']),
      frame(
        'running',
        26,
        '표적과 ADMET을 병렬로 분석하고 있습니다.',
        {
          target_hypothesis: 'running',
          admet: 'running',
          dta: 'pending',
          decision: 'pending',
        },
        ['Target Hypothesis 시작', 'ADMET 시작'],
      ),
      frame(
        'running',
        68,
        'DTA 없이 확보된 근거로 판단을 계속합니다.',
        {
          target_hypothesis: 'completed',
          admet: 'completed',
          dta: 'skipped',
          decision: 'running',
        },
        ['표적 근거 완료', 'DTA 입력 부족으로 건너뜀', 'Decision 시작'],
        {
          target_hypothesis: '표적 근거는 확보했지만 유효한 단백질 서열이 없습니다.',
          dta: '표적 서열이 없어 결합 예측을 실행하지 않았습니다.',
        },
      ),
      frame(
        'partial_failure',
        100,
        '일부 데이터 공백을 남긴 채 분석을 완료했습니다.',
        {
          target_hypothesis: 'completed',
          admet: 'completed',
          dta: 'skipped',
          decision: 'completed',
        },
        ['Decision 완료', '부분 실패로 종료'],
        { dta: '결합 근거가 없으며 음성 예측으로 해석하면 안 됩니다.' },
      ),
    ],
    outcome: {
      label: 'Partial failure',
      title: '현재 근거로 조건부 검토가 가능합니다.',
      description: 'DTA 결과가 없다는 사실과 필요한 후속 검증을 최종 결과에 함께 표시합니다.',
    },
  },
  failed: {
    id: 'failed',
    label: '전체 실패',
    description: '전문 근거를 확보하지 못해 Decision을 실행하지 않습니다.',
    frames: [
      frame('ready', 0, '실행 전', pendingStages, ['분석 입력 검증 완료']),
      frame(
        'running',
        28,
        '표적과 ADMET을 병렬로 분석하고 있습니다.',
        {
          target_hypothesis: 'running',
          admet: 'running',
          dta: 'pending',
          decision: 'pending',
        },
        ['Target Hypothesis 시작', 'ADMET 시작'],
      ),
      frame(
        'failed',
        100,
        '판단에 사용할 전문 근거를 확보하지 못했습니다.',
        {
          target_hypothesis: 'failed',
          admet: 'failed',
          dta: 'skipped',
          decision: 'skipped',
        },
        ['Target Hypothesis 실패', 'ADMET 실패', 'Decision 실행 안 함'],
        {
          target_hypothesis: '표적 근거 제공자가 응답하지 않았습니다.',
          admet: 'ADMET 실행 환경을 사용할 수 없습니다.',
          dta: '표적 입력이 없어 실행하지 않았습니다.',
          decision: '사용 가능한 전문 근거가 없어 실행하지 않았습니다.',
        },
      ),
    ],
    outcome: {
      label: 'Failed',
      title: '이번 실행에서는 판정을 만들 수 없습니다.',
      description: '실제 화면에서는 재시도 가능 여부와 실패한 단계를 안내합니다.',
    },
  },
}

export const mockScenarioList = Object.values(mockScenarios)
