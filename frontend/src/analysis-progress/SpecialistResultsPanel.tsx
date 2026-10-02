import type { ReactNode } from 'react'
import type { Analysis } from './api'
import type { SpecialistResults, SpecialistRun } from './specialistResults'
import { downloadSpecialistResultsReview } from './specialistResultsReview'
import { formatScientificNumber } from './scientificNumber'
import './SpecialistResults.css'

const statuses: Record<string, string> = {
  queued: '대기', running: '실행 중', completed: '완료', partial_failure: '부분 완료',
  failed: '실패', skipped: '건너뜀', succeeded: '예측 완료',
}
const verdicts = {
  go: { title: 'Go', description: '추가 연구 진행' },
  conditional_go: { title: 'Conditional Go', description: '조건부 추가 연구' },
  no_go: { title: 'No-Go', description: '추가 연구 보류' },
}
const agentNames = { target_hypothesis: 'Target', admet: 'ADMET', dta: 'DTA', decision: 'Decision' }
const purposes = { initial: '초기 분석', evidence_followup: '추가 근거 응답', reassessment: '근거 반영 재판단', unverified: '후속 호출 (관계 미확인)' }
const channels = { herg: 'hERG', nav1_5: 'Nav1.5', cav1_2: 'Cav1.2' }
const assaySources = { bindingdb: 'BindingDB', chembl: 'ChEMBL', pubchem: 'PubChem BioAssay' }
const evidenceRelationships = {
  CONCORDANT: '예측과 실험근거가 일관됨',
  DECISION_RELEVANT_DISAGREEMENT: '모델이 potency 기준의 서로 다른 영역을 지지함',
  PREDICTION_EXPERIMENT_CONFLICT: '예측과 직접 비교 가능한 실험값이 충돌함',
  EXPERIMENT_SOURCE_CONFLICT: '실험 데이터베이스 간 결과가 충돌함',
  INSUFFICIENT_EXPERIMENTAL_EVIDENCE: '판단에 필요한 실험근거가 부족함',
  NOT_COMPARABLE: '현재 기준으로 직접 비교할 수 없음',
}

export function SpecialistResultsPanel({ data, analysis, afterDecision }: {
  data: SpecialistResults
  analysis: Analysis
  afterDecision?: ReactNode
}) {
  const admet = data.admet.result, dta = data.dta.result, decision = data.decision.result
  return <div className="specialist-results">
    <div className="specialist-results__heading">
      <h3>분석 결과</h3>
      {data.status !== 'completed' && <span className={`specialist-results__status specialist-results__status--${data.status}`} role="status">
        {statuses[data.status]}
      </span>}
      <button className="secondary-button specialist-results__download" type="button"
        onClick={() => downloadSpecialistResultsReview(data, analysis)}>
        결과 JSON 다운로드
      </button>
    </div>
    <Card title="Decision" run={data.decision} featured>
      {decision && <>
        <h5 className={`specialist-results__verdict specialist-results__verdict--${decision.verdict}`}>
          <span>{verdicts[decision.verdict].title}</span>{' '}
          <small>{verdicts[decision.verdict].description}</small>
        </h5>
        {decision.assessment ? <>
          <p className="specialist-results__rationale">{decision.headline}</p>
          <DecisionOverview data={data} analysis={analysis} />
          <SectionItems title="핵심 강점" items={decision.key_strengths ?? []} />
          <SectionItems title="주요 우려" items={decision.key_concerns ?? []} />
          <SectionItems title="상충하는 근거" items={decision.conflicts} />
          <SectionItems title="판정을 바꿀 수 있는 확인 사항" items={decision.gaps} />
          <NextActions actions={decision.next_actions} />
        </> : <>
          <p className="specialist-results__rationale">{decision.rationale}</p>
          <NextActions actions={decision.next_actions} />
          <Items title="상충하는 근거" items={decision.conflicts} />
          <Items title="근거 공백과 추가 확인" items={decision.gaps} />
        </>}
        <details><summary>{decision.assessment ? '세부 근거와 종합 문장' : '판단 범위와 근거 추적'}</summary>
          {decision.assessment && <p>{decision.rationale}</p>}
          <Items title="누락된 단계" items={decision.missing_stages} />
          <Items title="추가 연구 진행 제한 사유" items={decision.server_metadata?.go_restrictions.map((r) => restrictionText(r.cause)) ?? []} />
          <p>연구 우선순위에 한정된 판단입니다. 임상 효능·안전성, 치료 권고 또는 승인 여부를 뜻하지 않습니다.</p>
          <p>사용한 근거 ID: {decision.used_evidence_ids.join(', ') || '없음'}</p>
          <p>원본 실행 ID: {decision.source_run_ids.join(', ') || '없음'}</p>
        </details>
      </>}
    </Card>
    {afterDecision}
    <Card title="DTA" run={data.dta}>
      {dta && <>
        <Interpretation value={dta.interpretation} note={dta.interpretation_note} />
        <details><summary>DTA 예측값 해석 범위</summary><p>모델의 결합 예측이며 실험 측정값이나 약효 판정이 아닙니다. 서로 다른 점수·단위를 하나의 순위로 합치지 않습니다.</p></details>
        <div className="specialist-results__grid">{dta.candidates.map((candidate) => <article key={candidate.ensembl_id}>
          <h5>{candidate.approved_symbol} · {statuses[candidate.status]}</h5>
          <p>{candidate.ensembl_id} · {candidate.uniprot_accession}</p>
          {candidate.model_runs.length ? candidate.model_runs.map((run) => <section className="specialist-results__model-run" key={run.tool_id} aria-label={`${run.tool_id} 모델 실행`}>
            <h6>{run.model?.model_id ?? run.tool_id} · {statuses[run.status]}</h6>
            {run.observations.map((score, index) => <p key={index}><strong>{score.score_type}: {formatScientificNumber(score.value)}</strong> · {score.unit}</p>)}
            {run.error_code && <p>예측 결과 없음 · {run.error_code}</p>}
            {run.model && <p>모델: {run.model.provider} / {run.model.model_id} / {run.model.version}</p>}
            {run.tool_call_id && <details><summary>도구 호출 기록</summary><code>{run.tool_call_id}</code></details>}
          </section>) : <>
            {candidate.observations.map((score, index) => <p key={index}><strong>{score.score_type}: {formatScientificNumber(score.value)}</strong> · {score.unit}</p>)}
            {candidate.error_code && <p>예측 결과 없음 · {candidate.error_code}</p>}
            {candidate.model && <p>모델: {candidate.model.provider} / {candidate.model.model_id} / {candidate.model.version}</p>}
            {candidate.tool_call_id && <details><summary>도구 호출 기록</summary><code>{candidate.tool_call_id}</code></details>}
          </>}
          <DtaEvidence candidate={candidate} />
        </article>)}</div>
        <details><summary>타깃 원본 실행</summary><code>{dta.source_target_run_id}</code></details>
      </>}
    </Card>
    <Card title="ADMET" run={data.admet}>
      {admet && <>
        <p>전체 {admet.catalog_endpoint_count}개 중 {admet.selected_endpoint_count}개 항목의 선택 요약입니다.
          {admet.selection_is_subset && ' 일부 항목을 선택한 것은 실행 실패를 의미하지 않습니다.'}</p>
        <Interpretation value={admet.interpretation} note={admet.interpretation_note} />
        <Items title="누락된 항목" items={admet.missing_endpoints} />
        <ToxicityPriorities axes={admet.toxicity_axes} />
        <CardiacObservations data={data} />
        <details><summary>ADMET 예측값 해석 범위</summary><p>예측값은 임상 안전성 판정이 아닙니다. 백분위는 기준 집단 내 위치이며 신뢰도나 성공 확률이 아닙니다.</p></details>
        <details><summary>선택 항목별 예측값 ({admet.endpoints.length})</summary>
          <div className="specialist-results__grid">{admet.endpoints.map((endpoint) => <article key={endpoint.endpoint_id}>
            <h5>{endpoint.endpoint_id} · {endpoint.name}</h5>
            <dl><dt>예측값</dt><dd>{formatScientificNumber(endpoint.value)} · {endpoint.units ?? '단위 미지정'}</dd>
              <dt>분류 / 모델 유형</dt><dd>{endpoint.category} / {endpoint.task_type}</dd>
              <dt>DrugBank 승인 약물 기준 백분위</dt><dd>{formatScientificNumber(endpoint.drugbank_approved_percentile)} / 100</dd>
              <dt>종</dt><dd>{endpoint.species ?? '정보 없음'}</dd></dl>
            {endpoint.source_url && <Source url={endpoint.source_url} />}
          </article>)}</div>
        </details>
        <Items title="ADMET 한계" items={admet.limitations} />
        <details><summary>ADMET 출처와 버전</summary><p>도구 {admet.tool_version} · {admet.reference_population}</p>
          <p>원본 실행 <code>{admet.source_run_id}</code></p><p>도구 호출 <code>{admet.source_tool_call_id}</code></p></details>
      </>}
    </Card>
    {data.calls.length > 0 && <section className="specialist-results__card" aria-label="Agent 호출 흐름">
      <h4>Agent 호출 흐름</h4>
      <p>번호는 분석 전체가 아닌 Agent별 호출 순서입니다. 요청·응답 연결은 저장된 실행 관계만 표시합니다.</p>
      <ol className="specialist-results__calls">{data.calls.map((call) => <li key={call.run_id}>
        <strong>{agentNames[call.agent_name]} {call.call_number}번</strong> · {purposes[call.purpose]}
        {' · '}{call.status ? statuses[call.status] : '상태 없음'}
        {call.request && <p>요청: {call.request.objective} <small>({call.request.gap_kind})</small></p>}
        {call.purpose === 'evidence_followup' && call.responds_to_run_id && <p>Decision 요청에 대한 응답</p>}
        {call.purpose === 'reassessment' && call.responds_to_run_id && <p>추가 근거를 반영한 재판단</p>}
        {call.response_run_id && <p>후속 응답 실행 연결됨</p>}
        {call.cardiac_result && <>
          <p>심장 이온채널 예측 · {call.cardiac_result.execution_mode === 'live' ? '실행' : '재생'} · {call.cardiac_result.tool_version}</p>
          <ul>{call.cardiac_result.predictions.map((prediction) => <li key={prediction.channel}>
            CToxPred2 · {channels[prediction.channel]} · {prediction.label === 'positive' ? '차단 예측' : '비차단 예측'} · 모델이 출력한 {prediction.label === 'positive' ? '차단' : '비차단'} 분류 확률 {formatScientificNumber(prediction.class_probability)}
          </li>)}</ul>
          {call.cardiac_result.limitations.length > 0 && <Items title="심장 독성 예측의 한계" items={call.cardiac_result.limitations} />}
        </>}
        {call.error_code && <p>실행 오류 코드: <code>{call.error_code}</code></p>}
        <details><summary>실행 관계 ID</summary>
          <p>실행 ID: <code>{call.run_id}</code></p>
          {call.triggering_run_id && <p>시작 근거: <code>{call.triggering_run_id}</code></p>}
          {call.responds_to_run_id && <p>응답 대상: <code>{call.responds_to_run_id}</code></p>}
          {call.response_run_id && <p>후속 응답: <code>{call.response_run_id}</code></p>}
        </details>
      </li>)}</ol>
      <details><summary>호출 기록 해석 범위</summary><p>모델 분류 확률은 임상 심독성 발생 확률이 아닙니다. 호출 기록은 새 분석을 실행하지 않습니다.</p></details>
    </section>}
    <details className="specialist-results__metadata">
      <summary>분석 정보</summary>
      <p>분석 상태: {statuses[data.status]}</p>
      <p>분석 ID: <code>{data.analysis_id}</code></p>
    </details>
  </div>
}

type DtaCandidate = NonNullable<SpecialistResults['dta']['result']>['candidates'][number]

const regionLabels = {
  same_region_meets_reference: '두 모델 모두 기준선 이상',
  same_region_below_reference: '두 모델 모두 기준선 미만',
  split_across_reference: '두 모델의 판단이 기준선을 사이에 두고 갈림',
  insufficient_model_results: '예측 정보 부족',
}
const executionIssues = {
  provider_disabled: '실측 조회 꺼짐', tool_budget_exhausted: '호출 한도 초과', execution_failed: '실측 조회 실패',
}
const areaLabels = { target: '표적–질환', dta: '결합 예측', adme: 'ADME', safety: '안전성' }
const areaStatuses: Record<string, string> = { supported: '지지됨', unresolved: '확인 필요', informative: '참고', unavailable: '정보 없음' }
const priorityLabels = { priority_check: '우선 확인', not_priority: '현재 우선 확인 대상 아님', missing: '평가 정보 부족' }

function regionText(assessment: NonNullable<DtaCandidate['evidence_assessment']>) {
  if (!assessment.criterion || assessment.criterion.endpoint !== 'Kd') return '비교 기준 없음(Kd 기준만 지원)'
  return assessment.region_status ? regionLabels[assessment.region_status] : '예측 정보 부족'
}

function restrictionText(cause: string) {
  if (cause === 'no_supported_target') return '질환 근거와 조절 방향이 함께 지지되는 표적 후보가 없습니다.'
  if (cause === 'recall_failed') return '추가 조사를 완료하지 못했습니다.'
  const [, stage] = cause.split(':')
  const title = stage === 'target_hypothesis' ? '표적' : stage === 'dta' ? '결합 예측' : 'ADMET'
  return cause.startsWith('stage_missing:') ? `${title} 단계의 결과가 없습니다.` : `${title} 단계가 부분 완료되어 추가 확인이 필요합니다.`
}

function ToxicityPriorities({ axes }: { axes: NonNullable<SpecialistResults['admet']['result']>['toxicity_axes'] }) {
  if (!axes) return null
  return <section aria-label="독성 확인시험 우선순위"><h5>독성 확인시험 우선순위</h5>
    <ul>{axes.axes.map((axis) => <li key={axis.axis}>
      {axis.endpoint_id} · <span className={`specialist-results__area-status specialist-results__area-status--${axis.priority_status === 'priority_check' ? 'unresolved' : axis.priority_status === 'missing' ? 'unavailable' : 'informative'}`}>{priorityLabels[axis.priority_status]}</span>
      {axis.value !== null && <> · 예측값 {formatScientificNumber(axis.value)} · 승인 약물 백분위 {formatScientificNumber(axis.drugbank_approved_percentile!)}</>}
    </li>)}</ul>
    <p>확인시험의 우선순위이며 안전·음성 판정을 의미하지 않습니다.</p>
  </section>
}

function CardiacObservations({ data }: { data: SpecialistResults }) {
  const observations = data.calls.filter((call) => call.cardiac_result)
  if (!observations.length) return null
  return <section aria-label="심장 이온통로 추가 조사"><h5>추가 조사로 보강됨</h5>
    {observations.map((call) => <ul key={call.run_id}>{call.cardiac_result!.predictions.map((p) => <li key={p.channel}>
      CToxPred2 · {channels[p.channel]} · {p.label === 'positive' ? '차단 예측' : '비차단 예측'} · 모델이 출력한 {p.label === 'positive' ? '차단' : '비차단'} 분류 확률 {formatScientificNumber(p.class_probability)}
    </li>)}</ul>)}
    <p>추가 모델의 분류 확률과 ADMET hERG 예측값은 서로 다른 척도입니다.</p>
  </section>
}

function DecisionOverview({ data, analysis }: { data: SpecialistResults; analysis: Analysis }) {
  const decision = data.decision.result!
  const areas = decision.assessment!
  const metadata = decision.server_metadata
  const multiple = (metadata?.candidate_gates.length ?? 0) > 1
  const targets = analysis.target_prioritization
    ? [analysis.target_prioritization.primary, ...analysis.target_prioritization.alternatives] : []
  const directions: Record<string, string> = { inhibit: '억제', activate: '활성화', stabilize: '안정화', unknown: '미확정' }
  const causality: Record<string, string> = { supported: '지지됨', conflicting: '상충', unknown: '미확정' }
  return <>
    <h5>평가 요약</h5>
    <div className="specialist-results__grid">{(Object.keys(areaLabels) as Array<keyof typeof areaLabels>).map((area) => <article key={area} aria-label={`${areaLabels[area]} 평가`}>
      <h5>{areaLabels[area]} <span className={`specialist-results__area-status specialist-results__area-status--${areas[area].status}`}>{areaStatuses[areas[area].status]}</span></h5>
      {multiple && (area === 'target' || area === 'dta') && <p>대표 후보: {metadata!.lead_candidate?.symbol}</p>}
      <p>{areas[area].summary}</p>
      <dl>{areas[area].key_values.map((value, i) => <div key={i}><dt>{value.label}</dt><dd><strong>{formatScientificNumber(value.value)}{value.unit && ` ${value.unit}`}</strong></dd></div>)}</dl>
      {area === 'safety' && <>
        {areas[area].status === 'supported' && <p>평가한 세 축 기준</p>}
        <ToxicityPriorities axes={data.admet.result?.toxicity_axes ?? null} />
        <CardiacObservations data={data} />
      </>}
    </article>)}</div>
    {multiple && <section aria-label="표적 후보별 상태"><h5>표적 후보별 상태</h5>
      <table className="specialist-results__candidate-table"><thead><tr><th>후보</th><th>표적–질환 근거</th><th>결합 예측</th></tr></thead>
        <tbody>{metadata!.candidate_gates.map((g) => {
          const target = targets.find((t) => t.ensembl_id === g.ensembl_id)
          const dta = data.dta.result?.candidates.find((c) => c.ensembl_id === g.ensembl_id)
          return <tr key={g.ensembl_id} className={g.ensembl_id === metadata!.lead_candidate?.ensembl_id ? 'specialist-results__lead-row' : ''}>
            <th scope="row">{g.symbol}{g.ensembl_id === metadata!.lead_candidate?.ensembl_id && ' · 대표 후보'}</th>
            <td>{target ? `${causality[target.causal_support.status]} · ${directions[target.causal_support.therapeutic_direction]}` : '정보 없음'}</td>
            <td>{dta?.model_runs.map((r) => <p key={r.tool_id}>{r.model?.model_id ?? r.tool_id}: {r.observations.map((o) => formatScientificNumber(o.value)).join(', ') || '정보 없음'}</p>)}
              <p>{dta?.evidence_assessment ? regionText(dta.evidence_assessment) : '정보 없음'}</p>
              {dta?.evidence_assessment?.experimental_binding_support && <p>정량 실측 결합 지지</p>}
            </td>
          </tr>
        })}</tbody></table>
    </section>}
  </>
}

function DtaEvidence({ candidate }: { candidate: DtaCandidate }) {
  const assessment = candidate.evidence_assessment
  return <section className="specialist-results__assay" aria-label={`${candidate.approved_symbol} 실험근거`}>
    <h6>동일 compound–target 실험근거</h6>
    {assessment ? <>
      <p className={`specialist-results__relationship specialist-results__relationship--${assessment.status.toLowerCase()}`}>
        {assessment.region_status ? regionText(assessment) : evidenceRelationships[assessment.status]}
      </p>
      {assessment.pubchem_execution_issue && <p>{executionIssues[assessment.pubchem_execution_issue]}</p>}
      {assessment.experimental_binding_support && <p>기준선에 부합하는 정량 실측 결합 근거가 확보되었습니다.</p>}
      <dl className="specialist-results__recall-status">
        <div><dt>PubChem 추가 확인 요청</dt><dd>{assessment.pubchem_requested ? '예' : '아니오'}</dd></div>
        <div><dt>실행</dt><dd>{assessment.pubchem_executed ? '호출 시도함' : '실행 안 됨'}</dd></div>
        <div><dt>근거 공백 해소</dt><dd>{assessment.pubchem_resolved ? '해소됨' : '해소되지 않음'}</dd></div>
      </dl>
      {assessment.recall_trigger && <p>추가 확인 사유: {assessment.recall_trigger === 'model_decision_region_disagreement' ? '두 결합 모델의 기준선 판단 불일치' : '과거 조회 정책에 따른 추가 확인'}</p>}
      <Items title="관계 판정의 한계" items={assessment.limitations} />
    </> : <p>이 저장 결과에는 실험근거 관계 판정이 없습니다.</p>}
    {candidate.experimental_evidence.length ? <details open>
      <summary>확인된 assay ({candidate.experimental_evidence.length})</summary>
      <ul className="specialist-results__assay-list">{candidate.experimental_evidence.map((evidence) =>
        <li key={`${evidence.source}:${evidence.source_record_id}`}>
          <strong>{assaySources[evidence.source]}</strong>
          <span>{evidence.kind === 'quantitative'
            ? `${evidence.endpoint} ${formatScientificNumber(evidence.value!)} ${evidence.unit}`
            : `정성 결과: ${evidence.qualitative_outcome}`}</span>
          {evidence.assay_description && <p>{evidence.assay_description}</p>}
          <small>원본 레코드: <code>{evidence.source_record_id}</code></small>
          {(evidence.doi || evidence.pmid) && <p className="specialist-results__references">
            {evidence.doi && <>DOI {evidence.doi}</>}
            {evidence.doi && evidence.pmid && ' · '}
            {evidence.pmid && <>PMID {evidence.pmid}</>}
          </p>}
        </li>)}</ul>
    </details> : <p>exact compound–target 조건을 충족한 공개 assay가 없습니다.</p>}
    <Items title="외부 데이터 조회 오류" items={candidate.evidence_errors} />
    <p className="specialist-results__assay-note">
      공개 DB에서 결과가 없거나 조회가 실패한 것은 결합하지 않는다는 의미가 아닙니다.
    </p>
  </section>
}

function Card({ title, run, children, featured = false }: { title: string; run: SpecialistRun<unknown>; children: ReactNode; featured?: boolean }) {
  return <section className={`specialist-results__card${featured ? ' specialist-results__card--featured' : ''}`} aria-label={`${title} 저장 결과`}>
    <h4>{title} <small>{run.status ? statuses[run.status] : '실행 기록 없음'}</small></h4>
    {run.projection_status !== 'available' && <p>{run.projection_status === 'invalid'
      ? '저장 결과의 형식을 검증하지 못해 세부 결과를 표시하지 않습니다.'
      : '표시할 저장 결과가 없습니다. 완료된 결과를 임의로 대체하지 않습니다.'}</p>}
    {run.error_code && <p>{run.error_code === 'decision_dta_assay_policy_mismatch'
      ? '과거 결합 근거 조회 정책의 결과는 새 판정에 사용할 수 없습니다. DTA부터 다시 실행해야 합니다.'
      : <>실행 오류 코드: <code>{run.error_code}</code></>}</p>}
    {children}
    <Items title="경고 코드" items={run.warning_codes} />
    <details><summary>{title} 실행 기록과 토큰</summary>
      <p>실행 ID: <code>{run.run_id ?? '없음'}</code></p>
      <p>원본 실행의 토큰 사용량 (이번 조회 비용 아님): {run.token_usage
        ? `입력 ${run.token_usage.input_tokens} / 출력 ${run.token_usage.output_tokens} / 합계 ${run.token_usage.total_tokens}`
        : '정보 없음'}</p>
    </details>
  </section>
}

function Interpretation({ value, note }: {
  value: { summary: string; limitations: string[]; used_evidence_ids: string[] } | null; note: string
}) {
  return <div><h5>Agent 해석</h5>{value ? <p>{value.summary}</p>
    : <p>제공할 수 있는 해석이 없습니다. 예측값의 존재 여부와는 별개입니다.</p>}
    <details><summary>해석 범위와 근거</summary><p>{note}</p>{value && <>
      {value.limitations.length > 0 && <ul>{value.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>}
      <p>사용한 근거 ID: {value.used_evidence_ids.join(', ') || '없음'}</p>
    </>}</details></div>
}

function Items({ title, items }: { title: string; items: string[] }) {
  return items.length ? <details><summary>{title} ({items.length})</summary>
    <ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul></details> : null
}

function SectionItems({ title, items }: { title: string; items: string[] }) {
  return items.length ? <section aria-label={title}><h5>{title}</h5>
    <ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul></section> : null
}

function NextActions({ actions }: {
  actions: Array<{ status: 'proposed'; action: string; rationale: string; decision_impact: string }>
}) {
  return <section className="specialist-results__next-actions" aria-labelledby="decision-next-actions-heading">
    <div className="specialist-results__next-actions-heading">
      <h5 id="decision-next-actions-heading">다음 연구 행동</h5>
      <span>제안</span>
    </div>
    <p className="specialist-results__next-actions-notice">
      아래 내용은 아직 수행되지 않은 제안입니다. 시스템의 실제 실행·완료 기록은 Agent 호출 흐름에서 별도로 확인할 수 있습니다.
    </p>
    {actions.length ? <ol>{actions.map((item, index) => <li key={index}>
      <article>
        <h6>제안 {index + 1}</h6>
        <p className="specialist-results__next-action-task">{item.action}</p>
        <dl>
          <div><dt>제안 이유</dt><dd>{item.rationale}</dd></div>
          <div><dt>판정에 미치는 영향</dt><dd>{item.decision_impact}</dd></div>
        </dl>
      </article>
    </li>)}</ol> : <p className="specialist-results__next-actions-empty">
      이 저장 결과에는 구조화된 다음 행동이 제공되지 않았습니다.
    </p>}
  </section>
}

function Source({ url }: { url: string }) {
  let safe = false
  try {
    safe = ['http:', 'https:'].includes(new URL(url).protocol)
  } catch { /* 잘못된 URL은 링크로 만들지 않는다. */ }
  return safe ? <a href={url} target="_blank" rel="noopener noreferrer">데이터 출처 (새 탭)</a>
    : <p>출처 링크를 표시할 수 없습니다.</p>
}
