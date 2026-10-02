import { describe, expect, it } from 'vitest'
import fixture from './fixtures/cdk4-api-summary.json'
import { parseTargetPrioritization } from './targetPrioritization'

describe('target prioritization public contract', () => {
  it('preserves the actual CDK4 API projection without scientific reinterpretation', () => {
    expect(parseTargetPrioritization(fixture)).toEqual(fixture)
  })
  it.each([undefined, null])('handles missing and null older summaries', (value) => {
    expect(parseTargetPrioritization(value)).toBeNull()
  })
  it('uses unknown causal support for an older pre-causal response', () => {
    const { causal_support, causal_rationale, causal_evidence_ids, ...oldPrimary } = fixture.primary
    expect(causal_support).toBeDefined()
    expect(causal_rationale).toBeDefined()
    expect(causal_evidence_ids).toBeDefined()
    const result = parseTargetPrioritization({ ...fixture, primary: oldPrimary })
    expect(result?.primary.causal_support.status).toBe('unknown')
    expect(result?.primary.causal_support.therapeutic_direction).toBe('unknown')
  })
  it('keeps older pre-Pharos responses compatible', () => {
    const { pharos_evidence, ...oldPrimary } = fixture.primary
    expect(pharos_evidence).toBeDefined()
    const result = parseTargetPrioritization({ ...fixture, primary: oldPrimary })
    expect(result?.primary.pharos_evidence).toBeNull()
  })
  it('projects only public fields even when unknown fields contain a protein sequence', () => {
    const data = {
      ...fixture,
      target_sequence: 'PRIVATE_SEQUENCE',
      primary: { ...fixture.primary, target_sequence: 'PRIVATE_SEQUENCE' },
    }
    const serialized = JSON.stringify(parseTargetPrioritization(data))
    expect(serialized).not.toContain('PRIVATE_SEQUENCE')
    expect(serialized).not.toContain('target_sequence')
  })
  it.each([-0.01, 1.01, NaN, Infinity, '0.63', null])(
    'rejects invalid association score %s',
    (association_score) => {
      expect(() =>
        parseTargetPrioritization({
          ...fixture,
          primary: { ...fixture.primary, association_score },
        }),
      ).toThrow()
    },
  )
  it.each([
    { eligibility: 'successful' },
    { rank: 0 },
    { rank: 1.5 },
    { ensembl_id: 'invalid' },
    { uniprot_accession: 'invalid-id' },
    { tractability_assessments: [{ label: 'Approved Drug', value: 'true' }] },
    { pharos_evidence: { ...fixture.primary.pharos_evidence, development_level: 'Tready' } },
    { pharos_evidence: { ...fixture.primary.pharos_evidence, ligand_count: -1 } },
    { pharos_evidence: { ...fixture.primary.pharos_evidence, novelty: Number.NaN } },
    { pharos_evidence: { ...fixture.primary.pharos_evidence, retrieved_at: 'not-a-date' } },
    { causal_support: null },
    { causal_support: { status: 'proven' } },
    { causal_support: { biological_evidence_count: -1 } },
    { causal_support: { therapeutic_direction: 'cure' } },
    { causal_support: { evidence: [{ evidence_id: 'only-an-id' }] } },
    { modulation_action: 'block' },
    { prioritization_rationale: '' },
  ])('rejects malformed nested candidate fields: %j', (changes) => {
    expect(() =>
      parseTargetPrioritization({ ...fixture, primary: { ...fixture.primary, ...changes } }),
    ).toThrow()
  })
  it('rejects duplicate ranks and candidates', () => {
    expect(() =>
      parseTargetPrioritization({ ...fixture, alternatives: [fixture.primary] }),
    ).toThrow()
  })
})
