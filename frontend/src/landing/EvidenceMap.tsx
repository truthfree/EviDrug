import './EvidenceMap.css'

/** 분석 입력부터 근거 검토, 다음 연구 판단까지의 흐름을 보여준다. */
export function EvidenceMap() {
  return (
    <div className="evidence-map" aria-label="EviDrug 분석 흐름">
      <div className="evidence-map__header">
        <span>Analysis flow</span>
        <span>01 — 03</span>
      </div>

      <div className="evidence-map__content">
        <div className="evidence-map__stage-label">연구 질문 입력</div>
        <div className="evidence-map__inputs">
          <div className="evidence-map__input">
            <span className="evidence-map__input-icon" aria-hidden="true">01</span>
            <strong>질환 맥락</strong>
            <small>질환 · 아형</small>
          </div>
          <span className="evidence-map__plus" aria-hidden="true">+</span>
          <div className="evidence-map__input">
            <span className="evidence-map__input-icon" aria-hidden="true">02</span>
            <strong>화합물</strong>
            <small>분자 구조</small>
          </div>
        </div>

        <div className="evidence-map__connector" aria-hidden="true" />

        <div className="evidence-map__stage-label">독립적인 근거 검토</div>
        <div className="evidence-map__lenses">
          <div className="evidence-map__lens evidence-map__lens--target">
            <span>01</span>
            <strong>표적 타당성</strong>
          </div>
          <div className="evidence-map__lens evidence-map__lens--binding">
            <span>02</span>
            <strong>결합 가능성</strong>
          </div>
          <div className="evidence-map__lens evidence-map__lens--admet">
            <span>03</span>
            <strong>ADMET</strong>
          </div>
        </div>

        <div className="evidence-map__connector" aria-hidden="true" />

        <div className="evidence-map__decision">
          <span>다음 연구 판단</span>
          <strong>후속 검증의 우선순위</strong>
          <small>지지 근거 · 불확실성 · 다음 실험</small>
        </div>
      </div>
    </div>
  )
}
