import { type FormEvent, useState } from 'react'
import './AccessCodePanel.css'

type AccessCodePanelProps = {
  errorMessage: string | null
  isSubmitting: boolean
  onAccessCodeChange: () => void
  onCancel: () => void
  onSubmit: (accessCode: string) => Promise<boolean>
}

/** 분석 기능에 진입하기 위한 공용 접근 코드를 입력받는다. */
export function AccessCodePanel({
  errorMessage,
  isSubmitting,
  onAccessCodeChange,
  onCancel,
  onSubmit,
}: AccessCodePanelProps) {
  const [accessCode, setAccessCode] = useState('')
  const [showAccessCode, setShowAccessCode] = useState(false)

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (accessCode.length === 0 || isSubmitting) return

    const signedIn = await onSubmit(accessCode)
    if (signedIn) setAccessCode('')
  }

  return (
    <section className="access-panel" aria-labelledby="access-title">
      <button className="text-button access-panel__back" type="button" onClick={onCancel}>
        <ArrowLeftIcon />
        서비스 안내로 돌아가기
      </button>

      <div className="access-panel__heading">
        <span className="access-panel__number" aria-hidden="true">
          01
        </span>
        <div>
          <p className="section-label">Protected workspace</p>
          <h2 id="access-title">분석 작업공간에 들어가기</h2>
        </div>
      </div>

      <p className="access-panel__description">
        <span>분석 작업공간을 이용하려면</span>{' '}
        <span>공용 접근 코드를 입력해 주세요.</span>
      </p>

      <form className="access-form" onSubmit={handleSubmit}>
        <label htmlFor="access-code">접근 코드</label>
        <div className={`access-input ${errorMessage ? 'access-input--error' : ''}`}>
          <KeyIcon />
          <input
            id="access-code"
            name="access-code"
            type={showAccessCode ? 'text' : 'password'}
            value={accessCode}
            autoComplete="current-password"
            autoFocus
            aria-describedby={errorMessage ? 'access-code-error' : undefined}
            aria-invalid={errorMessage ? 'true' : 'false'}
            placeholder="접근 코드를 입력하세요"
            disabled={isSubmitting}
            onChange={(event) => {
              setAccessCode(event.target.value)
              onAccessCodeChange()
            }}
          />
          <button
            className="access-input__toggle"
            type="button"
            aria-label={showAccessCode ? '접근 코드 숨기기' : '접근 코드 표시하기'}
            aria-pressed={showAccessCode}
            onClick={() => setShowAccessCode((isVisible) => !isVisible)}
          >
            {showAccessCode ? <EyeOffIcon /> : <EyeIcon />}
          </button>
        </div>

        {errorMessage && (
          <p id="access-code-error" className="form-error" role="alert">
            <WarningIcon />
            <span>{errorMessage}</span>
          </p>
        )}

        <button
          className="primary-button access-form__submit"
          type="submit"
          disabled={accessCode.length === 0 || isSubmitting}
        >
          {isSubmitting ? (
            <>
              <span className="button-spinner" aria-hidden="true" />
              확인 중
            </>
          ) : (
            <>
              계속
              <ArrowRightIcon />
            </>
          )}
        </button>
      </form>

    </section>
  )
}

function ArrowLeftIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="m15 18-6-6 6-6" />
    </svg>
  )
}

function ArrowRightIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="m9 18 6-6-6-6" />
    </svg>
  )
}

function KeyIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="8" cy="15" r="4" />
      <path d="m11 12 7-7m-2 2 2 2m-5 1 2 2" />
    </svg>
  )
}

function EyeIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" />
      <circle cx="12" cy="12" r="2.5" />
    </svg>
  )
}

function EyeOffIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="m3 3 18 18M10.6 6.1A10.3 10.3 0 0 1 12 6c6 0 9.5 6 9.5 6a17 17 0 0 1-2.1 2.8M6.2 6.2C3.8 7.8 2.5 12 2.5 12s3.5 6 9.5 6a9 9 0 0 0 3-.5M9.9 9.9a3 3 0 0 0 4.2 4.2" />
    </svg>
  )
}

function WarningIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 3 2.8 20h18.4L12 3Z" />
      <path d="M12 9v4m0 3h.01" />
    </svg>
  )
}
