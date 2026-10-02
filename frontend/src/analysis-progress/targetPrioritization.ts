/** 공개 polling 계약만 보존한다. 단백질 서열과 내부 output은 투영하지 않는다. */
export type TargetAction = 'inhibit' | 'activate' | 'stabilize' | 'unknown'
export type CausalEvidence = {
  evidence_id: string
  datasource_id: string
  datatype_id: string
  axis:
    'statistical_genetics' | 'clinical_genetics' | 'somatic' | 'functional' | 'clinical_validation'
  score: number
  disease_id: string
  disease_name: string
  disease_scope: 'direct' | 'subtype'
  direction_on_target: 'loss_of_function' | 'gain_of_function' | 'unknown'
  direction_on_trait: 'risk' | 'protective' | 'unknown'
  target_role: string | null
  confidence: string | null
  significant_driver_methods: string[]
}
export type CausalSupport = {
  status: 'supported' | 'conflicting' | 'unknown'
  reason_codes: string[]
  biological_evidence_count: number
  clinical_validation_count: number
  therapeutic_direction: TargetAction
  evidence: CausalEvidence[]
}
export type PharosEvidence = {
  uniprot_accession: string
  development_level: 'Tclin' | 'Tchem' | 'Tbio' | 'Tdark'
  target_family: string | null
  novelty: number | null
  ligand_count: number
  publication_count: number
  source_version: string
  retrieved_at: string
}
export type TargetRecommendation = {
  rank: number
  ensembl_id: string
  approved_symbol: string
  uniprot_accession: string
  association_score: number
  eligibility: 'eligible' | 'exploratory' | 'ineligible'
  eligibility_reason_codes: string[]
  tractability_assessments: { label: string; value: boolean }[]
  pharos_evidence: PharosEvidence | null
  causal_support: CausalSupport
  causal_evidence_ids: string[]
  modulation_action: TargetAction
  prioritization_rationale: string
  causal_rationale: string
}
export type ExcludedTarget = {
  ensembl_id: string
  approved_symbol: string
  association_score: number
  reason_code: string
}
export type TargetPrioritization = {
  modality: 'small_molecule'
  primary: TargetRecommendation
  alternatives: TargetRecommendation[]
  excluded_candidates: ExcludedTarget[]
  source_version: string
}

/** 오래된 응답의 생략/null은 허용하되, 존재하는 잘못된 결과는 성공으로 보정하지 않는다. */
export function parseTargetPrioritization(value: unknown): TargetPrioritization | null {
  if (value === undefined || value === null) return null
  const data = record(value)
  const primary = recommendation(data.primary)
  const alternatives = list(data.alternatives, recommendation)
  const shortlist = [primary, ...alternatives]
  if (
    primary.rank !== 1 ||
    new Set(shortlist.map((item) => item.rank)).size !== shortlist.length ||
    new Set(shortlist.map((item) => item.ensembl_id)).size !== shortlist.length
  )
    invalid()
  return {
    modality: choice(data.modality, ['small_molecule']),
    primary,
    alternatives,
    excluded_candidates: list(data.excluded_candidates, (item) => {
      const excluded = record(item)
      return {
        ensembl_id: identifier(excluded.ensembl_id, /^ENSG\d+$/),
        approved_symbol: text(excluded.approved_symbol, 120),
        association_score: score(excluded.association_score),
        reason_code: text(excluded.reason_code, 120),
      }
    }),
    source_version: text(data.source_version, 120),
  }
}

function recommendation(value: unknown): TargetRecommendation {
  const data = record(value)
  const rank = count(data.rank)
  if (rank < 1 || rank > 5) invalid()
  const evidenceIds =
    data.causal_evidence_ids === undefined
      ? []
      : list(data.causal_evidence_ids, (id) => text(id, 500))
  if (evidenceIds.length > 20) invalid()
  return {
    rank,
    ensembl_id: identifier(data.ensembl_id, /^ENSG\d+$/),
    approved_symbol: text(data.approved_symbol, 120),
    uniprot_accession: identifier(data.uniprot_accession, /^[A-Z0-9]+$/),
    association_score: score(data.association_score),
    eligibility: choice(data.eligibility, ['eligible', 'exploratory', 'ineligible']),
    eligibility_reason_codes: list(data.eligibility_reason_codes, (code) => text(code, 120)),
    tractability_assessments: list(data.tractability_assessments, (assessment) => {
      const entry = record(assessment)
      if (typeof entry.value !== 'boolean') invalid()
      return { label: text(entry.label, 120), value: entry.value }
    }),
    pharos_evidence: pharosEvidence(data.pharos_evidence),
    causal_support: causalSupport(data.causal_support),
    causal_evidence_ids: evidenceIds,
    modulation_action: action(data.modulation_action),
    prioritization_rationale: text(data.prioritization_rationale, 2000),
    causal_rationale:
      data.causal_rationale === undefined
        ? '개별 인과 근거가 제공되지 않아 인과성을 판단하지 않았습니다.'
        : text(data.causal_rationale, Infinity, true),
  }
}

function pharosEvidence(value: unknown): PharosEvidence | null {
  if (value === undefined || value === null) return null
  const data = record(value)
  const novelty = data.novelty === null ? null : nonnegative(data.novelty)
  return {
    uniprot_accession: identifier(data.uniprot_accession, /^[A-Z0-9]+$/),
    development_level: choice(data.development_level, ['Tclin', 'Tchem', 'Tbio', 'Tdark']),
    target_family: optionalText(data.target_family, 200),
    novelty,
    ligand_count: count(data.ligand_count),
    publication_count: count(data.publication_count),
    source_version: text(data.source_version, 120),
    retrieved_at: timestamp(data.retrieved_at),
  }
}

function causalSupport(value: unknown): CausalSupport {
  const data = value === undefined ? {} : record(value)
  return {
    status:
      data.status === undefined
        ? 'unknown'
        : choice(data.status, ['supported', 'conflicting', 'unknown']),
    reason_codes:
      data.reason_codes === undefined
        ? ['causal_evidence_missing']
        : list(data.reason_codes, (code) => text(code, 120)),
    biological_evidence_count:
      data.biological_evidence_count === undefined ? 0 : count(data.biological_evidence_count),
    clinical_validation_count:
      data.clinical_validation_count === undefined ? 0 : count(data.clinical_validation_count),
    therapeutic_direction:
      data.therapeutic_direction === undefined ? 'unknown' : action(data.therapeutic_direction),
    evidence: data.evidence === undefined ? [] : list(data.evidence, evidence),
  }
}

function evidence(value: unknown): CausalEvidence {
  const data = record(value)
  return {
    evidence_id: text(data.evidence_id, 500),
    datasource_id: text(data.datasource_id, 120),
    datatype_id: text(data.datatype_id, 120),
    axis: choice(data.axis, [
      'statistical_genetics',
      'clinical_genetics',
      'somatic',
      'functional',
      'clinical_validation',
    ]),
    score: score(data.score),
    disease_id: text(data.disease_id, 80),
    disease_name: text(data.disease_name, 200),
    disease_scope: choice(data.disease_scope, ['direct', 'subtype']),
    direction_on_target: choice(data.direction_on_target, [
      'loss_of_function',
      'gain_of_function',
      'unknown',
    ]),
    direction_on_trait: choice(data.direction_on_trait, ['risk', 'protective', 'unknown']),
    target_role: optionalText(data.target_role),
    confidence: optionalText(data.confidence),
    significant_driver_methods:
      data.significant_driver_methods === undefined
        ? []
        : list(data.significant_driver_methods, (method) => text(method)),
  }
}

function action(value: unknown): TargetAction {
  return choice(value, ['inhibit', 'activate', 'stabilize', 'unknown'])
}
function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalid()
  return value as Record<string, unknown>
}
function text(value: unknown, max = Infinity, empty = false): string {
  if (typeof value !== 'string' || (!empty && !value.trim()) || value.length > max) invalid()
  return value
}
function optionalText(value: unknown, max = Infinity): string | null {
  return value === undefined || value === null ? null : text(value, max, true)
}
function identifier(value: unknown, pattern: RegExp): string {
  const result = text(value)
  if (!pattern.test(result)) invalid()
  return result
}
function count(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) invalid()
  return value
}
function score(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) invalid()
  return value
}
function nonnegative(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) invalid()
  return value
}
function timestamp(value: unknown): string {
  const result = text(value)
  if (!Number.isFinite(Date.parse(result))) invalid()
  return result
}
function choice<const T extends readonly string[]>(value: unknown, allowed: T): T[number] {
  if (typeof value !== 'string' || !allowed.includes(value)) invalid()
  return value
}
function list<T>(value: unknown, parse: (item: unknown) => T): T[] {
  if (!Array.isArray(value)) invalid()
  return value.map(parse)
}
function invalid(): never {
  throw new Error('Invalid target prioritization response')
}
