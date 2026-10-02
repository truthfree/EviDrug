import type {
  CausalEvidence,
  CausalSupport,
  TargetAction,
  TargetRecommendation,
} from './targetPrioritization'

export const actionLabels: Record<TargetAction, string> = {
  inhibit: '억제',
  activate: '활성화',
  stabilize: '안정화',
  unknown: '판단 보류',
}
export const eligibilityLabels: Record<TargetRecommendation['eligibility'], string> = {
  eligible: '저분자로 표적화할 수 있다는 약물 선례·구조적 근거가 확인됨',
  exploratory: '탐색 후보 · 추가 검증 필요',
  ineligible: '저분자 표적화 기준 미충족',
}
export const causalLabels: Record<CausalSupport['status'], string> = {
  supported: '인과 근거 지지',
  conflicting: '인과 근거 충돌',
  unknown: '인과성 판단 정보 부족',
}
export const axisLabels: Record<CausalEvidence['axis'], string> = {
  statistical_genetics: '통계 유전학',
  clinical_genetics: '임상 유전학',
  somatic: '체세포 변이',
  functional: '기능 교란',
  clinical_validation: '임상 검증',
}
export const targetEffectLabels: Record<CausalEvidence['direction_on_target'], string> = {
  gain_of_function: '기능 증가 (GoF)',
  loss_of_function: '기능 감소 (LoF)',
  unknown: '기능 방향 불명',
}
export const traitEffectLabels: Record<CausalEvidence['direction_on_trait'], string> = {
  risk: '질환 위험 증가',
  protective: '질환 보호 방향',
  unknown: '질환 효과 불명',
}
const reasonLabels: Record<string, string> = {
  approved_small_molecule: '승인 저분자 약물 선례',
  advanced_clinical_small_molecule: '후기 임상 저분자 약물 선례',
  phase_1_clinical_small_molecule: '1상 임상 저분자 약물 선례',
  structure_with_ligand: '리간드 결합 구조 확인',
  high_quality_ligand: '고품질 리간드 근거',
  high_quality_pocket: '고품질 결합 포켓 근거',
  medium_quality_pocket: '중간 품질 결합 포켓 근거',
  druggable_family: '약물 표적화 가능 단백질 계열',
  small_molecule_tractability_evidence_missing: '저분자 표적화 가능성 근거 부족',
  reviewed_protein_unavailable: '검토된 단백질 정보 없음',
  target_sequence_not_verified: '단백질 서열 검증 미완료',
  not_shortlisted: '이번 추천 목록에 포함되지 않음',
  statistical_genetic_evidence_present: '통계 유전학 근거 있음',
  clinical_genetic_evidence_present: '임상 유전학 근거 있음',
  somatic_driver_evidence_present: '체세포 드라이버 근거 있음',
  functional_perturbation_evidence_present: '기능 교란 근거 있음',
  clinical_validation_present: '임상 검증 근거 있음',
  direct_disease_evidence_present: '입력 질환의 직접 근거 있음',
  subtype_evidence_present: '질환 하위 유형 근거 포함',
  causal_evidence_missing: '개별 인과 근거 없음',
  therapeutic_direction_conflicting: '치료 방향 근거 충돌',
  therapeutic_direction_unknown: '치료 방향을 판단할 근거 부족',
}
const tractabilityLabels: Record<string, string> = {
  'Approved Drug': '승인 약물',
  'Advanced Clinical': '후기 임상',
  'Phase 1 Clinical': '1상 임상',
  'Structure with Ligand': '리간드 결합 구조',
  'High-Quality Ligand': '고품질 리간드',
  'High-Quality Pocket': '고품질 결합 포켓',
  'Med-Quality Pocket': '중간 품질 결합 포켓',
  'Druggable Family': '표적화 가능 단백질 계열',
}
/** 새 서버 코드도 숨기지 않고 원래 식별자와 함께 확인할 수 있게 한다. */
export function reasonLabel(code: string): string {
  return reasonLabels[code] ?? `추가 사유: ${code}`
}
export function tractabilityLabel(label: string): string {
  return tractabilityLabels[label] ?? label
}
