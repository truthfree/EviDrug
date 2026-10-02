import { useState, type ChangeEvent, type FormEvent } from 'react'
import type { DiseaseCandidate } from '../disease-search/api'
import {
  AnalysisInputRequestError,
  DEFAULT_POTENCY_CRITERION,
  validateAnalysisInput,
  type AnalysisInputResponse,
  type TargetMode,
} from './api'
import './AnalysisInputPanel.css'

type AnalysisInputPanelProps = {
  disease: DiseaseCandidate
  onValidatedInputChange: (input: AnalysisInputResponse | null) => void
}

type SubmissionState =
  | { status: 'idle'; result: null; message: null }
  | { status: 'submitting'; result: null; message: null }
  | { status: 'success'; result: AnalysisInputResponse; message: null }
  | { status: 'error'; result: null; message: string }

const initialSubmissionState: SubmissionState = {
  status: 'idle',
  result: null,
  message: null,
}

const EXAMPLE_TARGET_NAME = 'CDK4'
const EXAMPLE_SMILES =
  'CC1=C(C(=O)N(C2=NC(=NC=C12)NC3=NC=C(C=C3)N4CCNCC4)C5CCCC5)C(=O)C'

/** 확정된 질환에 사용할 타깃 방식과 화합물 SMILES를 입력받는다. */
export function AnalysisInputPanel({ disease, onValidatedInputChange }: AnalysisInputPanelProps) {
  const [targetMode, setTargetMode] = useState<TargetMode | null>(null)
  const [targetName, setTargetName] = useState('')
  const [smiles, setSmiles] = useState('')
  const [submission, setSubmission] = useState<SubmissionState>(initialSubmissionState)

  const normalizedTargetName = targetName.trim()
  const normalizedSmiles = smiles.trim()
  const canSubmit =
    targetMode !== null &&
    normalizedSmiles.length > 0 &&
    (targetMode === 'discover' || normalizedTargetName.length > 0) &&
    submission.status !== 'submitting'

  function resetValidation() {
    setSubmission(initialSubmissionState)
    onValidatedInputChange(null)
  }

  function handleTargetModeChange(mode: TargetMode) {
    setTargetMode(mode)
    if (mode === 'discover') setTargetName('')
    resetValidation()
  }

  function handleTargetNameChange(event: ChangeEvent<HTMLInputElement>) {
    setTargetName(event.target.value)
    resetValidation()
  }

  function handleSmilesChange(event: ChangeEvent<HTMLTextAreaElement>) {
    setSmiles(event.target.value)
    resetValidation()
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canSubmit || targetMode === null) return

    setSubmission({ status: 'submitting', result: null, message: null })
    try {
      const result = await validateAnalysisInput({
        disease_id: disease.id,
        disease_name: disease.name,
        target_mode: targetMode,
        ...(targetMode === 'specified' ? { target_name: normalizedTargetName } : {}),
        smiles: normalizedSmiles,
        potency_criterion: DEFAULT_POTENCY_CRITERION,
      })
      setSubmission({ status: 'success', result, message: null })
      onValidatedInputChange(result)
    } catch (error) {
      setSubmission({ status: 'error', result: null, message: toUserMessage(error) })
      onValidatedInputChange(null)
    }
  }

  return (
    <section className="analysis-input-card" aria-labelledby="analysis-input-title">
      <div className="analysis-input-heading">
        <span>02–03</span>
        <div>
          <p className="section-label">Target and compound</p>
          <h2 id="analysis-input-title">어떤 조건으로 화합물을 살펴볼까요?</h2>
          <p>타깃 지정 여부를 선택하고 분석할 화합물의 SMILES를 입력해 주세요.</p>
        </div>
      </div>

      <div className="confirmed-disease-summary">
        <span>확정된 질환</span>
        <strong>{disease.name}</strong>
        <code>{disease.id}</code>
      </div>

      <form className="analysis-input-form" onSubmit={(event) => void handleSubmit(event)}>
        <fieldset className="target-mode-fieldset">
          <legend>검토할 타깃이 정해져 있나요?</legend>
          <div className="target-mode-options">
            <label className={targetMode === 'discover' ? 'target-mode-option--selected' : undefined}>
              <input
                type="radio"
                name="target-mode"
                value="discover"
                checked={targetMode === 'discover'}
                onChange={() => handleTargetModeChange('discover')}
              />
              <span>
                <strong>타깃 없이 탐색</strong>
                <small>확정한 질환을 기준으로 후보 타깃을 찾습니다.</small>
              </span>
            </label>
            <label className={targetMode === 'specified' ? 'target-mode-option--selected' : undefined}>
              <input
                type="radio"
                name="target-mode"
                value="specified"
                checked={targetMode === 'specified'}
                onChange={() => handleTargetModeChange('specified')}
              />
              <span>
                <strong>특정 타깃 평가</strong>
                <small>관심 있는 타깃을 질환 맥락에서 검토합니다.</small>
              </span>
            </label>
          </div>
        </fieldset>

        {targetMode === 'specified' && (
          <div className="analysis-input-field">
            <label htmlFor="target-name">Target name</label>
            <input
              id="target-name"
              type="text"
              value={targetName}
              maxLength={200}
              placeholder="e.g. BACE1"
              autoComplete="off"
              required
              onChange={handleTargetNameChange}
            />
            <p className="analysis-input-example">
              Demo Guide <code>{EXAMPLE_TARGET_NAME}</code>
            </p>
            <p>현재는 입력한 이름을 보존하며 표준 타깃 ID 확인은 다음 단계에서 진행합니다.</p>
          </div>
        )}

        <div className="analysis-input-field">
          <label htmlFor="compound-smiles">SMILES</label>
          <textarea
            id="compound-smiles"
            value={smiles}
            maxLength={4096}
            rows={4}
            placeholder="e.g. CC(=O)OC1=CC=CC=C1C(=O)O"
            spellCheck={false}
            required
            onChange={handleSmilesChange}
          />
          <p className="analysis-input-example">
            Demo Guide <code>{EXAMPLE_SMILES}</code>
          </p>
          <p>분자 구조로 해석할 수 있는지 확인하며 데이터베이스 등록 여부는 제한하지 않습니다.</p>
        </div>

        <section className="potency-criterion-notice" aria-label="DTA 결합친화도 기준">
          <strong>DTA 고정 기준 · Kd ≤ 100 nM (pKd ≥ 7.0)</strong>
          <p>
            두 DTA 모델의 후보 선별 영향을 비교하기 위한 PoC 기준입니다. 사용자가 변경할 수
            없으며, 유방암 특이적 최적값이나 보편적인 lead 기준은 아닙니다.
          </p>
        </section>

        {submission.status === 'error' && (
          <p className="analysis-input-message analysis-input-message--error" role="alert">
            {submission.message}
          </p>
        )}

        {submission.status === 'success' && (
          <div className="analysis-input-result" role="status">
            <span aria-hidden="true">✓</span>
            <div>
              <strong>분석 입력을 확인했습니다.</strong>
              <span>Canonical SMILES</span>
              <code>{submission.result.canonical_smiles}</code>
              <span>적용할 DTA 기준</span>
              <code>
                Kd ≤ {submission.result.potency_criterion?.maximum_value}{' '}
                {submission.result.potency_criterion?.unit}
              </code>
            </div>
          </div>
        )}

        <button className="analysis-input-submit" type="submit" disabled={!canSubmit}>
          {submission.status === 'submitting' ? '입력 확인 중' : '분석 입력 확인'}
        </button>
      </form>
    </section>
  )
}

function toUserMessage(error: unknown): string {
  if (error instanceof AnalysisInputRequestError) {
    if (error.code === 'invalid_smiles') {
      return '분자 구조로 해석할 수 없는 SMILES입니다. 괄호, 결합과 원자가 표현을 확인해 주세요.'
    }
    if (error.code === 'invalid_session') {
      return '세션이 만료되었습니다. 로그아웃 후 다시 로그인해 주세요.'
    }
    if (error.code === 'invalid_input') {
      return '타깃 방식과 입력값의 조합을 확인해 주세요.'
    }
  }
  return '분석 입력을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.'
}
