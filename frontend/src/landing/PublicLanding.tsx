import { useState } from 'react'
import { AccessCodePanel } from '../authentication/AccessCodePanel'
import { Brand } from '../shared/Brand'
import { ArrowRightIcon, FlaskIcon, LockIcon } from '../shared/Icons'
import { EvidenceMap } from './EvidenceMap'
import './PublicLanding.css'

type PublicLandingProps = {
  isCheckingSession: boolean
  isServiceUnavailable: boolean
  serviceMessage: string | null
  onRetryConnection: () => void
  onSignIn: (accessCode: string) => Promise<boolean>
  isSigningIn: boolean
  signInError: string | null
  onClearSignInError: () => void
}

/** 인증 전에도 서비스 목적과 분석 방식을 이해할 수 있는 공개 화면이다. */
export function PublicLanding({
  isCheckingSession,
  isServiceUnavailable,
  serviceMessage,
  onRetryConnection,
  onSignIn,
  isSigningIn,
  signInError,
  onClearSignInError,
}: PublicLandingProps) {
  const [showAccessPanel, setShowAccessPanel] = useState(false)

  const openAccessPanel = () => setShowAccessPanel(true)
  const closeAccessPanel = () => {
    setShowAccessPanel(false)
    onClearSignInError()
  }

  return (
    <div className="site-shell">
      <SiteHeader
        sessionStatus={
          isCheckingSession ? 'checking' : isServiceUnavailable ? 'unavailable' : 'signed_out'
        }
        onStart={openAccessPanel}
      />

      {isServiceUnavailable && (
        <ConnectionBanner message={serviceMessage} onRetry={onRetryConnection} />
      )}

      <main>
        <section className="hero" aria-labelledby="service-title">
          <HeroCopy isCheckingSession={isCheckingSession} onStart={openAccessPanel} />

          <div className="hero__visual">
            {showAccessPanel ? (
              <AccessCodePanel
                errorMessage={signInError}
                isSubmitting={isSigningIn}
                onAccessCodeChange={onClearSignInError}
                onCancel={closeAccessPanel}
                onSubmit={onSignIn}
              />
            ) : (
              <EvidenceMap />
            )}
          </div>
        </section>

        <EvidenceOverview />
        <ResearchNotice />
      </main>

      <footer className="site-footer">
        <Brand />
        <p>Evidence-oriented decision support for drug discovery.</p>
      </footer>
    </div>
  )
}

type SessionStatus = 'checking' | 'signed_out' | 'unavailable'

function SiteHeader({ sessionStatus, onStart }: { sessionStatus: SessionStatus; onStart: () => void }) {
  const statusLabel = {
    checking: '세션 확인 중',
    signed_out: '접근 코드 필요',
    unavailable: '서비스 연결 필요',
  }[sessionStatus]

  return (
    <header className="site-header">
      <Brand />
      <nav aria-label="공개 화면">
        <a href="#evidence-title">분석 방식</a>
        <a href="#service-title">서비스 안내</a>
      </nav>
      <button className="header-access" type="button" onClick={onStart}>
        <span className={`status-dot status-dot--${sessionStatus}`} aria-hidden="true" />
        {statusLabel}
      </button>
    </header>
  )
}

function ConnectionBanner({ message, onRetry }: { message: string | null; onRetry: () => void }) {
  return (
    <div className="connection-banner" role="status">
      <span className="status-symbol status-symbol--warning" aria-hidden="true">
        !
      </span>
      <span>{message}</span>
      <button type="button" onClick={onRetry}>
        다시 연결
      </button>
    </div>
  )
}

function HeroCopy({ isCheckingSession, onStart }: { isCheckingSession: boolean; onStart: () => void }) {
  return (
    <div className="hero__copy">
      <p className="eyebrow">
        <span aria-hidden="true" />
        Evidence-guided discovery
      </p>
      <h1 id="service-title">
        <span className="hero__title-line">흩어진 근거를 연결해,</span>
        <span className="hero__title-line hero__title-line--accent">
          다음 결정을 선명하게.
        </span>
      </h1>
      <p className="hero__summary">
        질환과 화합물 사이의 복잡한 근거를 여러 전문 에이전트가 독립적으로 검토하고,
        판단의 이유와 불확실성을 함께 보여줍니다.
      </p>

      <div className="hero__actions">
        <button
          className="primary-button"
          type="button"
          disabled={isCheckingSession}
          onClick={onStart}
        >
          {isCheckingSession ? '세션 확인 중' : '분석 시작'}
          {isCheckingSession ? (
            <span className="button-spinner" aria-hidden="true" />
          ) : (
            <ArrowRightIcon />
          )}
        </button>
        <span className="hero__access-note">
          <LockIcon />
          공용 접근 코드가 필요합니다
        </span>
      </div>

      <dl className="hero__facts" aria-label="서비스 특징">
        <Fact value="4" label="독립적 분석 관점" />
        <Fact value="Traceable" label="근거와 판단 이력" />
        <Fact value="Human-led" label="사용자 최종 확인" />
      </dl>
    </div>
  )
}

function Fact({ value, label }: { value: string; label: string }) {
  return (
    <div>
      <dt>{value}</dt>
      <dd>{label}</dd>
    </div>
  )
}

const evidenceCards = [
  {
    number: '01',
    title: 'Target hypothesis',
    description: '질환을 명확한 개념으로 해석하고 표적의 생물학적 타당성을 구조화합니다.',
    accent: 'violet',
  },
  {
    number: '02',
    title: 'Binding affinity',
    description: '서로 다른 모델의 적용 범위와 결합 가능성을 비교해 한쪽의 확신에 치우치지 않습니다.',
    accent: 'blue',
  },
  {
    number: '03',
    title: 'ADMET profile',
    description: '약동학과 안전성 신호를 함께 살펴 개발 가능성을 넓은 맥락에서 평가합니다.',
    accent: 'green',
  },
  {
    number: '04',
    title: 'Evidence decision',
    description: '일치하는 근거와 충돌, 데이터 공백을 구분해 다음 검증 단계를 제안합니다.',
    accent: 'amber',
  },
] as const

function EvidenceOverview() {
  return (
    <section className="evidence-section" aria-labelledby="evidence-title">
      <div className="section-heading">
        <p className="section-label">One question · Four lenses</p>
        <h2 id="evidence-title">
          <span>하나의 결론보다,</span>
          <span>결론에 이르는 근거를 봅니다.</span>
        </h2>
      </div>

      <div className="evidence-grid">
        {evidenceCards.map((card) => (
          <EvidenceCard key={card.number} {...card} />
        ))}
      </div>
    </section>
  )
}

type EvidenceCardProps = (typeof evidenceCards)[number]

function EvidenceCard({ number, title, description, accent }: EvidenceCardProps) {
  return (
    <article className={`evidence-card evidence-card--${accent}`}>
      <div className="evidence-card__top">
        <span>{number}</span>
        <span className="evidence-card__node" aria-hidden="true" />
      </div>
      <h3>{title}</h3>
      <p>{description}</p>
    </article>
  )
}

function ResearchNotice() {
  return (
    <section className="research-note" aria-label="서비스 이용 안내">
      <div className="research-note__mark">
        <FlaskIcon />
      </div>
      <div className="research-note__copy">
        <p className="section-label">For research use</p>
        <h2>확정적인 답보다 검토 가능한 판단을 제공합니다.</h2>
        <p className="research-note__disclaimer">
          EviDrug의 결과는 연구 가설과 우선순위 결정을 지원하며, 임상 판단이나 치료 권고를
          대신하지 않습니다.
        </p>
      </div>
    </section>
  )
}
