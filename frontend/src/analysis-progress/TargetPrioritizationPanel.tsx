import { useId } from 'react'
import type { TargetPrioritization, TargetRecommendation } from './targetPrioritization'
import {
  actionLabels,
  axisLabels,
  causalLabels,
  eligibilityLabels,
  reasonLabel,
  targetEffectLabels,
  tractabilityLabel,
  traitEffectLabels,
} from './targetLabels'
import './TargetPrioritizationPanel.css'

const tdlLabels = {
  Tclin: '임상 표적',
  Tchem: '화학 도구 확인',
  Tbio: '생물학적 정보 확인',
  Tdark: '저연구 표적',
} as const

const tdlDescriptions = {
  Tclin: '승인된 의약품이 알려진 표적입니다.',
  Tchem: '기준을 충족하는 고활성 저분자 ligand가 알려진 표적입니다.',
  Tbio: '생물학적 정보는 축적됐지만 Tclin·Tchem 기준에는 해당하지 않는 표적입니다.',
  Tdark: '알려진 약물·활성 ligand와 생물학적 정보가 상대적으로 부족한 저연구 표적입니다.',
} as const

/** 저장된 Target 결과를 표시한다. 다른 단계 실패를 지우거나 최종 효능 판정으로 확대하지 않는다. */
export function TargetPrioritizationPanel({
  result,
  demo = false,
}: {
  result: TargetPrioritization | null
  demo?: boolean
}) {
  const headingId = useId()
  if (!result)
    return (
      <section className="target-results target-results--empty" aria-labelledby={headingId}>
        <h3 id={headingId}>타깃 후보와 근거</h3>
        <p>
          표시할 Target 요약이 아직 없습니다. 단계가 미완료됐거나 과거 실행에 요약이 없을 수
          있습니다.
        </p>
      </section>
    )
  const hasPharos = [result.primary, ...result.alternatives].some(
    (candidate) => candidate.pharos_evidence !== null,
  )
  return (
    <section className="target-results" aria-labelledby={headingId}>
      <header className="target-results__heading">
        <div>
          <p className="target-results__eyebrow">
            {demo ? 'Demo data · 저분자' : 'Target prioritization · 저분자'}
          </p>
          <h3 id={headingId}>타깃 후보와 근거</h3>
        </div>
        <span className="target-results__source">
          {demo ? 'UI fixture' : hasPharos ? 'Open Targets · Pharos' : 'Open Targets'} ·{' '}
          {result.source_version}
        </span>
      </header>
      <p className="target-results__notice">
        {demo
          ? '화면 검토용 데모입니다. CDK4 저장 예시와 합성 대안·제외 후보를 사용하며, 현재 입력의 실제 분석 결과가 아닙니다.'
          : 'Target 단계의 저장 결과입니다. 후속 단계의 성공 여부나 최종 약물 효능 판정을 뜻하지 않습니다.'}
      </p>
      <p className="target-results__hint">
        연관성 점수는 0–1 범위의 질환 관련성 지표이며, 성공 확률이나 약물 접근성 점수가 아닙니다.
      </p>
      <CandidateCard candidate={result.primary} primary />
      <h4 className="target-results__subheading">
        대안 후보 <span>{result.alternatives.length}</span>
      </h4>
      {result.alternatives.length ? (
        <div className="target-results__alternatives">
          {[...result.alternatives]
            .sort((a, b) => a.rank - b.rank)
            .map((candidate) => (
              <CandidateCard key={candidate.ensembl_id} candidate={candidate} />
            ))}
        </div>
      ) : (
        <p className="target-results__hint">이번 실행에는 대안 후보가 없습니다.</p>
      )}
      <details className="target-results__excluded">
        <summary>제외된 후보 ({result.excluded_candidates.length})</summary>
        <p className="target-results__hint">
          이번 추천 목록에서 제외됐다는 뜻이며, 생물학적 무효나 약물 개발 불가능을 의미하지
          않습니다.
        </p>
        {result.excluded_candidates.length ? (
          <ul>
            {result.excluded_candidates.map((candidate) => (
              <li key={candidate.ensembl_id}>
                <strong>{candidate.approved_symbol}</strong>
                <span>연관성 {candidate.association_score.toFixed(3)}</span>
                <p>{reasonLabel(candidate.reason_code)}</p>
                <code>{candidate.reason_code}</code>
              </li>
            ))}
          </ul>
        ) : (
          <p>기록된 제외 후보가 없습니다.</p>
        )}
      </details>
    </section>
  )
}

function CandidateCard({
  candidate,
  primary = false,
}: {
  candidate: TargetRecommendation
  primary?: boolean
}) {
  const headingId = useId()
  const support = candidate.causal_support
  const positives = candidate.tractability_assessments.filter((item) => item.value)
  const directionConflict = support.reason_codes.includes('therapeutic_direction_conflicting')
  return (
    <article
      className={`target-card${primary ? ' target-card--primary' : ''}`}
      aria-labelledby={headingId}
    >
      <header className="target-card__heading">
        <div>
          <span className="target-card__rank">
            {primary ? '우선 추천' : '대안 후보'} · {candidate.rank}순위
          </span>
          <h4 id={headingId}>{candidate.approved_symbol}</h4>
        </div>
        <span
          className={`target-card__eligibility target-card__eligibility--${candidate.eligibility}`}
        >
          {eligibilityLabels[candidate.eligibility]}
        </span>
      </header>
      <div className="target-card__identifiers">
        <span>Ensembl {candidate.ensembl_id}</span>
        <span>UniProt {candidate.uniprot_accession}</span>
      </div>
      <dl className="target-card__metrics">
        <div>
          <dt>질환 연관성</dt>
          <dd>
            {candidate.association_score.toFixed(3)} <small>/ 1</small>
          </dd>
        </div>
        <div>
          <dt>인과 근거</dt>
          <dd>{causalLabels[support.status]}</dd>
        </div>
        <div>
          <dt>정책 계산 방향</dt>
          <dd>{actionLabels[support.therapeutic_direction]}</dd>
        </div>
        <div>
          <dt>LLM 제안 방향</dt>
          <dd>{actionLabels[candidate.modulation_action]}</dd>
        </div>
      </dl>
      {candidate.pharos_evidence && (
        <section className="target-card__maturity" aria-label="Pharos 표적 성숙도">
          <div className="target-card__maturity-heading">
            <h5>표적 성숙도 · Pharos</h5>
            <span>{candidate.pharos_evidence.development_level}</span>
          </div>
          <p className="target-card__maturity-level">
            {tdlLabels[candidate.pharos_evidence.development_level]}
            {candidate.pharos_evidence.target_family
              ? ` · ${candidate.pharos_evidence.target_family}`
              : ''}
          </p>
          <p className="target-card__maturity-description">
            {tdlDescriptions[candidate.pharos_evidence.development_level]}
            {candidate.pharos_evidence.target_family
              ? ' Family는 Pharos가 분류한 표적 단백질 계열입니다.'
              : ''}
          </p>
          <dl className="target-card__maturity-metrics">
            <div>
              <dt>알려진 ligand</dt>
              <dd>{candidate.pharos_evidence.ligand_count.toLocaleString('ko-KR')}개</dd>
              <p>Pharos가 이 표적과 연결한 ligand 레코드 수입니다.</p>
            </div>
            <div>
              <dt>Publication</dt>
              <dd>{candidate.pharos_evidence.publication_count.toLocaleString('ko-KR')}건</dd>
              <p>Pharos가 이 표적과 연결한 문헌 수입니다.</p>
            </div>
            <div>
              <dt>Novelty</dt>
              <dd>
                {candidate.pharos_evidence.novelty === null
                  ? '제공되지 않음'
                  : candidate.pharos_evidence.novelty.toPrecision(3)}
              </dd>
              <p>
                PubMed 초록의 표적 언급 희소성에 기반한 TIN-X 점수입니다. 값이 높을수록 문헌 언급이
                적어 상대적으로 덜 연구된 표적입니다.
              </p>
            </div>
          </dl>
          <p className="target-results__hint">
            TDL과 지식량은 표적의 연구·개발 성숙도를 설명하며, 질환 인과성이나 임상 성공 확률을
            의미하지 않습니다. 건수는 근거의 품질·선택성이나 입력 약물의 결합을 보장하지 않습니다.
          </p>
          <details className="target-card__tdl-guide">
            <summary>TDL 등급 기준 보기</summary>
            <dl>
              {Object.entries(tdlDescriptions).map(([level, description]) => (
                <div key={level}>
                  <dt>{level}</dt>
                  <dd>{description}</dd>
                </div>
              ))}
            </dl>
          </details>
        </section>
      )}
      {directionConflict && (
        <p className="target-card__conflict">
          치료 방향 근거 충돌: 서로 다른 방향을 지지하는 근거가 있어 단일 조절 방향으로 확정할 수
          없습니다.
        </p>
      )}
      <div className="target-card__rationale">
        <h5>추천 이유 · LLM 해석</h5>
        <p>{candidate.prioritization_rationale}</p>
        <h5>인과성 해석 · LLM</h5>
        <p>{candidate.causal_rationale || '인과성 설명이 제공되지 않았습니다.'}</p>
      </div>
      <div className="target-card__tractability">
        <h5>저분자 표적화 가능성 근거</h5>
        {positives.length ? (
          <ul className="target-card__tags">
            {positives.map((item, index) => (
              <li key={`${item.label}-${index}`}>{tractabilityLabel(item.label)}</li>
            ))}
          </ul>
        ) : (
          <p>확인된 양성 항목이 없습니다. 추가 검증이 필요합니다.</p>
        )}
        <p className="target-results__hint">
          승인·임상 선례는 표적 수준의 정보이며, 입력 약물의 해당 질환 치료 승인을 뜻하지 않습니다.
        </p>
      </div>
      <details className="target-card__evidence">
        <summary>정책 사유와 개별 근거 보기</summary>
        <h5>저분자 표적화 정책 사유</h5>
        <ul>
          {candidate.eligibility_reason_codes.map((code, index) => (
            <li key={`${code}-${index}`}>
              {reasonLabel(code)} <code>{code}</code>
            </li>
          ))}
        </ul>
        <h5>인과성 정책 사유</h5>
        <ul>
          {support.reason_codes.map((code, index) => (
            <li key={`${code}-${index}`}>
              {reasonLabel(code)} <code>{code}</code>
            </li>
          ))}
        </ul>
        <p>
          생물학적 근거 {support.biological_evidence_count}건 · 임상 검증{' '}
          {support.clinical_validation_count}건
        </p>
        <p className="target-results__hint">
          인과 근거 지지는 인과성 확정이 아닙니다. 임상 검증 근거만으로 생물학적 인과성을 판단하지
          않습니다. 아래 목록은 조회된 근거 범위입니다.
        </p>
        {support.evidence.length ? (
          <ul className="target-card__evidence-list">
            {support.evidence.map((item, index) => (
              <li key={`${item.evidence_id}-${index}`}>
                <strong>
                  {axisLabels[item.axis]} · {item.datasource_id}
                </strong>
                <p>
                  {item.disease_name} ·{' '}
                  {item.disease_scope === 'direct' ? '입력 질환 직접 근거' : '하위 유형 근거'}
                </p>
                <p>
                  {targetEffectLabels[item.direction_on_target]} ·{' '}
                  {traitEffectLabels[item.direction_on_trait]}
                </p>
                <code>{item.evidence_id}</code>
                {candidate.causal_evidence_ids.includes(item.evidence_id) && (
                  <span className="target-card__cited">LLM이 인용한 근거</span>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p>조회된 개별 인과 근거가 없습니다.</p>
        )}
      </details>
      <p className="target-card__disclaimer">
        LLM 해석은 연구 가설이며 독립적인 결합·임상 근거 또는 치료 권고가 아닙니다.
      </p>
    </article>
  )
}
